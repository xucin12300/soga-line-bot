"""把 LINE 登記 bot 部署到 Synology NAS。

形狀跟 SogaMahjongGame/deploy/deploy.py 一樣（SFTP 增量同步 + SSH 重建容器），
各服務各自一支，部署 bot 不會碰到其他容器。

- 對外沒有自己的網域與 Cloudflare Tunnel。LINE 打的 webhook 是
  https://sogamj.com/api/line/webhook，由 SogaStatisticDashboardFrontend（公開
  站的 Next.js）透過共用的 soga-network 轉到 http://soga-line-bot:2160/callback，
  見那個 repo 的 app/api/line/webhook/route.ts。
- 區網上 publish 在 :12160，給計分電腦上的 flask soga 讀 /api/signups。
- 登記資料是 NAS 上 ${SYNO_BASE_DIR}/data/line_bot.db，重建容器不會不見。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import shlex
import sys
import time
from pathlib import Path

# 理由見 SogaMahjongGame/deploy/deploy.py 同一段：不換 utf-8 的話 compose 的
# 輸出會讓 Windows console 編碼炸掉；line_buffering 才看得到即時進度。
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

try:
    import paramiko
except ImportError as exc:
    raise SystemExit("請先安裝 paramiko：python -m pip install -r deploy/requirements.txt") from exc


ROOT_FILES = ["Dockerfile", ".dockerignore", "requirements.txt", "weekly_defaults.txt"]
SOURCE_DIRS = ["soga_line_bot"]
# 安全 build 工具，部署時跟 compose 一起上傳；跟其他部署 repo 的副本是同一份。
SAFE_BUILD_SCRIPT = Path(__file__).with_name("nas_safe_build.py")

SKIP_PARTS = {"__pycache__", ".venv", "data", "tests"}

DOCKER_COMPOSE = """services:
  line-bot:
    build:
      context: .
      dockerfile: Dockerfile
    # 固定容器名稱，公開站的 Next.js 靠它在 soga-network 上找到 webhook
    # （LINE_BOT_URL=http://soga-line-bot:2160）。
    container_name: soga-line-bot
    environment:
      LINE_CHANNEL_SECRET: ${LINE_CHANNEL_SECRET}
      LINE_CHANNEL_ACCESS_TOKEN: ${LINE_CHANNEL_ACCESS_TOKEN}
      SOGA_LINE_BOT_API_KEY: ${SOGA_LINE_BOT_API_KEY}
      LINE_ALLOWED_CHAT_IDS: ${LINE_ALLOWED_CHAT_IDS}
      LINE_ADMIN_USER_IDS: ${LINE_ADMIN_USER_IDS}
      LINE_READONLY_CHAT_IDS: ${LINE_READONLY_CHAT_IDS}
      SOGA_LINE_BOT_DB_PATH: /data/line_bot.db
      SOGA_PLAYERS_DB_PATH: /soga-data/soga.db
    volumes:
      - ./data:/data
      # 統計站那份 soga.db（scheduler-soga 每天從計分電腦同步過來），讀
      # history_player_info.current_name 當玩家名單。掛整個目錄而不是單一檔案：
      # 同步是整個檔案換掉，單檔 bind mount 會一直看到舊的那份。
      - ${SOGA_STATS_DATA_DIR}:/soga-data:ro
    ports:
      # 只綁 NAS 的區網位址：外面進來的路只有公開站代理的 webhook 那一條。
      - "${LINE_BOT_LAN_HOST}:12160:2160"
    networks:
      - default
      - soga-network
    restart: unless-stopped
    # 記憶體硬上限（memswap == mem，不准用 swap），理由見 2026-10-16 NAS 當機。
    mem_limit: 256m
    memswap_limit: 256m
    # 容器 log 設上限：Synology 預設的 db driver 不會輪替，一直長下去（跟其他
    # 服務一樣用 json-file 10MB × 3 份，最多約 30MB）。每一則 LINE 訊息都會留
    # 一行存取紀錄加一行指令紀錄，不設的話會無限膨脹。
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

networks:
  default: {}
  soga-network:
    external: true
"""


def log_step(message: str) -> None:
    print(f"\n==> [{time.strftime('%H:%M:%S')}] {message}")


def load_env(env_path: Path) -> None:
    """讀部署連線設定，已存在的環境變數優先。"""
    if not env_path.exists():
        raise SystemExit(
            f"找不到 {env_path}。把 deploy/deploy_synology.env.example 複製成這個檔名再填，"
            "或直接從 soga_live/deploy/.deploy_synology.env 複製一份、"
            "把 SYNO_BASE_DIR 改成 /volume1/homes/yc/soga-line-bot。"
        )
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"缺少必要的設定 {name}（見 deploy/deploy_synology.env.example）")
    return value


def validate_service_env(path: Path) -> dict[str, str]:
    """先驗設定，再碰 NAS。secret 填錯的話 LINE 送來的每一則都會被當成偽造的
    丟掉，群組裡看起來就是 bot 完全不理人——在本機先擋下來比較好查。"""
    if not path.exists():
        raise SystemExit(f"找不到 {path}。把 deploy/env.production.example 複製成這個檔名再填。")
    values = parse_env_file(path)
    required = (
        "LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "SOGA_LINE_BOT_API_KEY",
        "LINE_BOT_LAN_HOST",
        "SOGA_STATS_DATA_DIR",
    )
    missing = [name for name in required if not values.get(name)]
    if missing:
        raise SystemExit(f"{path} 缺少：{', '.join(missing)}")
    return values


def to_sftp_path(remote_path: str) -> str:
    """這台 NAS 的 SFTP session root 是 /volume1 本身（見 soga_live/deploy/deploy.py
    同名函式的說明）。"""
    volume_prefix = "/volume1/"
    if remote_path.startswith(volume_prefix):
        return remote_path[len(volume_prefix):]
    return remote_path


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    current = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def remote_matches(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str) -> bool:
    try:
        remote_stat = sftp.stat(remote_path)
    except FileNotFoundError:
        return False
    local_stat = local_path.stat()
    if remote_stat.st_size != local_stat.st_size:
        return False
    return int(remote_stat.st_mtime) >= int(local_stat.st_mtime) - 1


def upload(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str, counters: dict[str, int]) -> None:
    ensure_remote_dir(sftp, posixpath.dirname(remote_path))
    if remote_matches(sftp, local_path, remote_path):
        counters["skipped"] += 1
        return
    sftp.put(str(local_path), remote_path)
    local_stat = local_path.stat()
    sftp.utime(remote_path, (int(local_stat.st_atime), int(local_stat.st_mtime)))
    counters["uploaded"] += 1


def upload_text(sftp: paramiko.SFTPClient, content: str, remote_path: str) -> None:
    ensure_remote_dir(sftp, posixpath.dirname(remote_path))
    data = content.encode("utf-8")
    try:
        with sftp.file(remote_path, "rb") as remote_file:
            if remote_file.read() == data:
                print(f"Unchanged {remote_path}")
                return
    except FileNotFoundError:
        pass
    with sftp.file(remote_path, "wb") as remote_file:
        remote_file.write(data)
    print(f"Wrote {remote_path}")


def iter_uploadable_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        if SKIP_PARTS & set(path.parts):
            continue
        yield path


def sudo_run(ssh: paramiko.SSHClient, password: str, command: str, timeout: int = 2400, check: bool = False) -> str:
    """跑一條 NAS 上的 sudo 指令，輸出裡的密碼會被遮掉。check=True 時遠端失敗
    就丟例外，不會被當成成功。"""
    stdin, stdout, stderr = ssh.exec_command(f"sudo -S -p '' {command}", get_pty=True, timeout=timeout)
    stdin.write(password + "\n")
    stdin.flush()
    output = stdout.read().decode("utf-8", "replace")
    exit_status = stdout.channel.recv_exit_status()
    error = stderr.read().decode("utf-8", "replace")
    if error:
        output += "\n" + error
    output = output.replace(password, "[sudo-password]")
    if check and exit_status != 0:
        raise RuntimeError(f"遠端指令失敗（exit {exit_status}）：{command}\n{output}")
    return output


def ensure_docker_network(ssh: paramiko.SSHClient, password: str, network: str = "soga-network") -> None:
    probe = f"/usr/local/bin/docker network inspect {network} >/dev/null 2>&1; echo EXIT:$?"
    check = sudo_run(ssh, password, f"sh -lc {shlex.quote(probe)}")
    exit_lines = [line.strip() for line in check.splitlines() if line.strip().startswith("EXIT:")]
    if exit_lines and exit_lines[-1] == "EXIT:0":
        print(f"共用網路已存在：{network}")
        return
    print(sudo_run(ssh, password, f"/usr/local/bin/docker network create {network}"))


def curl(ssh: paramiko.SSHClient, password: str, url: str, extra: str = "") -> str:
    command = f'curl -s -m 15 {extra} -w "\\n[%{{http_code}}]" {shlex.quote(url)}'
    return sudo_run(ssh, password, f"sh -lc {shlex.quote(command)}")


def wait_for_health(ssh: paramiko.SSHClient, password: str, url: str, attempts: int = 12, delay: int = 5) -> str:
    """容器 Started 不代表 gunicorn 已經在聽，要等它真的回 200（麻將服務
    2026-09-22 就是太早打 health 而誤報部署失敗）。"""
    output = ""
    for attempt in range(1, attempts + 1):
        output = curl(ssh, password, url)
        if "[200]" in output:
            if attempt > 1:
                print(f"（等了 {(attempt - 1) * delay} 秒服務才開始聽）")
            return output
        if attempt < attempts:
            time.sleep(delay)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-upload", action="store_true", help="不同步檔案，只更新產生的設定、重建容器並驗收。")
    parser.add_argument("--no-cache", action="store_true", help="build 時不用快取。")
    args = parser.parse_args()

    deploy_root = Path(__file__).resolve().parent
    repo_root = deploy_root.parent

    load_env(deploy_root / ".deploy_synology.env")
    service_env_file = deploy_root / ".env.production"
    service_env = validate_service_env(service_env_file)

    host = require("SYNO_HOST")
    username = require("SYNO_USERNAME")
    password = require("SYNO_PASSWORD")
    base_dir = require("SYNO_BASE_DIR")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname=host, username=username, password=password, timeout=20)

    log_step("準備遠端目錄")
    # data/ 先建好並交給登入使用者：不然 docker 會用 root 建它，之後要把資料庫
    # 複製出來備份還得 sudo。
    sudo_run(ssh, password, f"mkdir -p {shlex.quote(base_dir + '/data')}")
    sudo_run(ssh, password, f"chown -R {shlex.quote(username)} {shlex.quote(base_dir)}")

    sftp_base = to_sftp_path(base_dir)
    sftp = ssh.open_sftp()
    if args.skip_upload:
        log_step("跳過檔案同步")
    else:
        counters = {"uploaded": 0, "skipped": 0}
        log_step("同步變更的檔案")
        for name in ROOT_FILES:
            upload(sftp, repo_root / name, f"{sftp_base}/{name}", counters)
        for dirname in SOURCE_DIRS:
            for path in iter_uploadable_files(repo_root / dirname):
                upload(sftp, path, f"{sftp_base}/{path.relative_to(repo_root).as_posix()}", counters)
        print(f"同步結果：上傳 {counters['uploaded']} 個、跳過 {counters['skipped']} 個")

    # 班底表的正本在 NAS 的 data/ 裡，使用者直接改那一份。只有第一次（NAS 上還
    # 沒有）才放上去，之後的部署絕對不能蓋掉別人改過的表。
    roster_remote = f"{sftp_base}/data/weekly_defaults.txt"
    try:
        sftp.stat(roster_remote)
        print(f"班底表已存在，不覆蓋：{roster_remote}")
    except FileNotFoundError:
        sftp.put(str(repo_root / "weekly_defaults.txt"), roster_remote)
        print(f"放上初始班底表：{roster_remote}")

    upload_text(sftp, DOCKER_COMPOSE, f"{sftp_base}/docker-compose.yml")
    upload_text(sftp, service_env_file.read_text(encoding="utf-8"), f"{sftp_base}/.env")
    upload_text(sftp, SAFE_BUILD_SCRIPT.read_text(encoding="utf-8"), f"{sftp_base}/nas_safe_build.py")
    sftp.close()

    log_step("確認共用的 Docker 網路存在")
    ensure_docker_network(ssh, password)

    log_step("重建容器")
    # 一律走 nas_safe_build.py（帶記憶體/CPU 上限與看門狗），不用
    # `docker-compose up --build`——2026-10-16 NAS 就是被那條路拖垮的。
    build_flags = "--memory 1g" + (" --no-cache" if args.no_cache else "")
    compose_command = (
        f"cd {shlex.quote(base_dir)} && /bin/python3 nas_safe_build.py {build_flags} line-bot "
        "&& /usr/local/bin/docker-compose up -d --no-build --no-deps line-bot"
    )
    try:
        print(sudo_run(ssh, password, f"sh -lc {shlex.quote(compose_command)}", timeout=3600, check=True))
    except RuntimeError as exc:
        ssh.close()
        raise SystemExit(f"docker-compose up 失敗：\n{exc}")

    log_step("檢查容器狀態")
    print(sudo_run(ssh, password, "/usr/local/bin/docker ps -a --filter name=soga-line-bot --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"))
    running = set(sudo_run(ssh, password, "/usr/local/bin/docker ps --filter status=running --format '{{.Names}}'").split())
    if "soga-line-bot" not in running:
        ssh.close()
        raise SystemExit("容器沒有進入 running 狀態。看上面的狀態表，並在 NAS 上跑 `docker logs soga-line-bot`。")

    log_step("驗收：/health")
    # 打區網位址而不是 127.0.0.1：埠口只 publish 在 LINE_BOT_LAN_HOST 上。
    health = wait_for_health(ssh, password, f"http://{service_env['LINE_BOT_LAN_HOST']}:12160/health")
    print(health)
    ssh.close()
    if "[200]" not in health:
        raise SystemExit("容器起來了但 /health 沒回 200——去看 `docker logs soga-line-bot`。")
    print("\n完成。LINE webhook 是 https://sogamj.com/api/line/webhook（公開站那側的代理也要部署過才通）。")


if __name__ == "__main__":
    main()
