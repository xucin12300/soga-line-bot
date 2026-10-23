#!/bin/python3
"""在 Synology NAS 上安全地 build compose 服務的映像。

2026-10-16 在 NAS 上跑 `docker-compose build frontend` 時整台 NAS 卡死（load
125、其中 I/O 119，連 ARP 都不回，只能強制關機）。`docker-compose build` 走
BuildKit，build 過程沒有任何記憶體或 CPU 上限，NAS 開始吃緊時也沒人喊停。

這支取代 `docker-compose up -d --build`：

1. 讀 `docker-compose config --format json`，拿到跟 compose 一模一樣的
   context / Dockerfile / build args / 映像名稱。
2. 用 classic builder（DOCKER_BUILDKIT=0）跑 `docker build`，因為只有它吃
   `--memory` / `--memory-swap` / `--cpuset-cpus`。每個 RUN 步驟（npm ci、
   next build……）都被關在上限裡，超過就是 build 失敗，不會拖垮整台。
3. 看門狗每 2 秒讀一次 /proc：dockerd 本身的記憶體暴增、可用記憶體太低、
   swap 開始暴增、或很多程序卡在磁碟 I/O 太久，就中止 build。2026-10-16
   的 kernel log 顯示當時長到 5.5GB 的是 dockerd（build 在 daemon 裡跑），
   不是 build 容器，所以第 2 點的 cgroup 上限擋不到，第一道防線是這裡。

build 成功後由呼叫端接著跑 `docker-compose up -d --no-build ...` 切換容器；
build 失敗或被中止時以非 0 結束，舊容器原封不動繼續服務。

NAS 上只有 Python 3.8，這支只用標準函式庫。四個部署 repo（soga_live、
SogaStatisticDashboardBackend、SogaMahjongGame、PoolBall）的 deploy/ 各放
一份相同的副本，部署時各自上傳；改一份記得同步其他三份。

用法（在 compose 專案目錄、以 root 執行）：
    python3 nas_safe_build.py [--profile tunnel] [選項] [service ...]
沒給 service 時，build 所有有 build 區塊的服務。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time

DOCKER = "/usr/local/bin/docker"
DOCKER_COMPOSE = "/usr/local/bin/docker-compose"


def log(message: str) -> None:
    print(f"[safe-build {time.strftime('%H:%M:%S')}] {message}", flush=True)


def read_meminfo() -> dict[str, int]:
    values = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, rest = line.split(":", 1)
            values[key] = int(rest.split()[0]) // 1024  # MB
    return values


def read_procs_blocked() -> int:
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("procs_blocked "):
                return int(line.split()[1])
    return 0


def dockerd_rss_mb() -> int:
    """dockerd 目前的 RSS（MB）；找不到時回 0。

    2026-10-16 的 kernel log：OOM 時被點名的一直是 dockerd 本身（anon-rss
    5.5GB，平常約 120MB）——build 的記憶體長在 daemon 裡，不在 build 容器的
    cgroup，`--memory` 管不到，只能靠這裡盯著。
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as f:
                if f.read().strip() != "dockerd":
                    continue
            with open(f"/proc/{entry}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            continue
    return 0


def swap_used_mb(mem: dict[str, int]) -> int:
    return mem["SwapTotal"] - mem["SwapFree"]


class Watchdog(threading.Thread):
    """NAS 一開始吃緊就中止 build。三個條件任一成立就觸發。"""

    def __init__(self, proc: subprocess.Popen, args: argparse.Namespace) -> None:
        super().__init__(daemon=True)
        self.proc = proc
        self.args = args
        self.reason: str | None = None
        self.stopped = threading.Event()

    def run(self) -> None:
        start_swap = swap_used_mb(read_meminfo())
        start_dockerd = dockerd_rss_mb()
        low_mem = 0
        blocked = 0
        last_report = 0.0
        while not self.stopped.wait(2):
            mem = read_meminfo()
            avail = mem["MemAvailable"]
            swap_growth = swap_used_mb(mem) - start_swap
            dockerd_growth = dockerd_rss_mb() - start_dockerd
            procs_blocked = read_procs_blocked()
            low_mem = low_mem + 1 if avail < self.args.min_available_mb else 0
            blocked = blocked + 1 if procs_blocked >= self.args.max_blocked else 0

            if time.time() - last_report >= 30:
                load = open("/proc/loadavg").read().split()[0]
                log(f"可用記憶體 {avail}MB，swap 增加 {swap_growth}MB，dockerd 增加 {dockerd_growth}MB，"
                    f"卡在 I/O 的程序 {procs_blocked}，load {load}")
                last_report = time.time()

            if dockerd_growth > self.args.max_dockerd_growth_mb:
                self.reason = (f"dockerd 比開始時多用了 {dockerd_growth}MB"
                               f"（上限 {self.args.max_dockerd_growth_mb}MB）")
            elif low_mem >= 3:
                self.reason = f"可用記憶體連續 6 秒低於 {self.args.min_available_mb}MB（現在 {avail}MB）"
            elif swap_growth > self.args.max_swap_growth_mb:
                self.reason = f"swap 比開始時多用了 {swap_growth}MB（上限 {self.args.max_swap_growth_mb}MB）"
            elif blocked >= self.args.blocked_seconds // 2:
                self.reason = (f"卡在磁碟 I/O 的程序連續 {self.args.blocked_seconds} 秒 ≥ "
                               f"{self.args.max_blocked}（現在 {procs_blocked}）")
            if self.reason:
                log(f"中止 build：{self.reason}")
                self.kill_build()
                return

    def kill_build(self) -> None:
        # classic builder 在 client 斷線時會取消 build，並因為 --force-rm 清掉
        # 中間容器。先 TERM，15 秒內沒結束再 KILL。
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def compose_config(profiles: list[str]) -> dict:
    cmd = [DOCKER_COMPOSE]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    return json.loads(subprocess.check_output(cmd))


def build_command(project: str, name: str, service: dict, args: argparse.Namespace) -> tuple[str, list[str]]:
    build = service["build"]
    unsupported = set(build) - {"context", "dockerfile", "args", "target"}
    if unsupported:
        raise SystemExit(f"{name} 的 build 區塊有這支還不支援的欄位：{sorted(unsupported)}")
    context = build["context"]
    dockerfile = build.get("dockerfile", "Dockerfile")
    if not os.path.isabs(dockerfile):
        dockerfile = os.path.join(context, dockerfile)
    # compose v2 沒寫 image 時的預設映像名稱就是「專案名-服務名」；用同一個
    # 名稱，`docker-compose up -d --no-build` 才會直接拿到這次 build 的結果。
    image = service.get("image") or f"{project}-{name}"
    cmd = [
        DOCKER, "build",
        "--memory", args.memory, "--memory-swap", args.memory,
        "--cpuset-cpus", args.cpuset,
        "--force-rm",
        "-t", image, "-f", dockerfile,
    ]
    if build.get("target"):
        cmd += ["--target", build["target"]]
    for key, value in sorted((build.get("args") or {}).items()):
        cmd += ["--build-arg", key if value is None else f"{key}={value}"]
    if args.no_cache:
        cmd.append("--no-cache")
    cmd.append(context)
    return image, cmd


def preflight(args: argparse.Namespace) -> None:
    mem = read_meminfo()
    if mem["MemAvailable"] < args.min_start_available_mb:
        raise SystemExit(f"NAS 現在可用記憶體只有 {mem['MemAvailable']}MB"
                         f"（至少要 {args.min_start_available_mb}MB），先不 build。")
    if swap_used_mb(mem) > args.max_start_swap_mb:
        raise SystemExit(f"NAS 已經用了 {swap_used_mb(mem)}MB swap"
                         f"（上限 {args.max_start_swap_mb}MB），先不 build。")
    log(f"開始前：可用記憶體 {mem['MemAvailable']}MB，swap 已用 {swap_used_mb(mem)}MB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("services", nargs="*")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只印出 docker build 指令，不執行（不需要 root）")
    parser.add_argument("--memory", default="2g", help="每個 RUN 步驟的記憶體上限（不含 swap）")
    parser.add_argument("--cpuset", default="2,3", help="build 只用這幾顆核心，其餘留給線上服務")
    parser.add_argument("--min-available-mb", type=int, default=1200)
    parser.add_argument("--max-swap-growth-mb", type=int, default=512)
    parser.add_argument("--max-dockerd-growth-mb", type=int, default=1536)
    parser.add_argument("--max-blocked", type=int, default=15)
    parser.add_argument("--blocked-seconds", type=int, default=20)
    parser.add_argument("--min-start-available-mb", type=int, default=2500)
    parser.add_argument("--max-start-swap-mb", type=int, default=1024)
    args = parser.parse_args()

    config = compose_config(args.profile)
    project = config["name"]
    services = config["services"]
    targets = args.services or [name for name, svc in services.items() if "build" in svc]
    for name in targets:
        if name not in services:
            raise SystemExit(f"compose 裡沒有 {name} 這個服務")
        if "build" not in services[name]:
            raise SystemExit(f"{name} 沒有 build 區塊，不需要 build")

    preflight(args)
    env = dict(os.environ, DOCKER_BUILDKIT="0")
    for name in targets:
        image, cmd = build_command(project, name, services[name], args)
        if args.dry_run:
            print("DOCKER_BUILDKIT=0 " + " ".join(shlex.quote(part) for part in cmd))
            continue
        log(f"build {name} → {image}（記憶體上限 {args.memory}，核心 {args.cpuset}）")
        started = time.time()
        proc = subprocess.Popen(cmd, env=env, start_new_session=True)
        watchdog = Watchdog(proc, args)
        watchdog.start()
        code = proc.wait()
        watchdog.stopped.set()
        watchdog.join()
        elapsed = int(time.time() - started)
        if watchdog.reason:
            log(f"{name} 的 build 已中止（{elapsed} 秒）：{watchdog.reason}。線上容器沒有被動到。")
            return 3
        if code != 0:
            log(f"{name} 的 build 失敗（exit {code}，{elapsed} 秒）。若訊息裡有 Killed / exit 137，"
                f"是撞到 --memory {args.memory} 上限。線上容器沒有被動到。")
            return code
        log(f"{name} build 完成（{elapsed} 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
