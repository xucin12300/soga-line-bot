"""SOGA 登記 bot：LINE webhook + 給 flask soga 讀的查詢 API。

路由：
- POST /callback      LINE 的 webhook。公開入口是 https://sogamj.com/api/line/webhook，
                      由統計站的 Next.js 原封不動轉過來（見那個 repo 的
                      app/api/line/webhook/route.ts）。
- GET  /api/signups   查登記與每週的表，給 flask soga（計分程式）用；要帶 X-Api-Key。
- GET  /health        健康檢查。

兩個群組共用同一張表：在哪一群打「+五晚」都登記到同一個地方，回覆裡附上整張
表。刻意不主動推播到另一群——推播按群組人數扣免費額度（每月 200 則），30 人
的群組推一次就是 30 則；回覆則是免費的。

設定全部走環境變數，見 .env.example。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, request

from . import parser
from .line_api import LineClient, mention, verify_signature
from .parser import TZ, WEEKDAYS, Command, Slot, week_start
from .players import SogaPlayers
from .store import SignupStore

logger = logging.getLogger(__name__)

# 固定班底：每週第一次用到那週的表時自動帶入，剩下的位子大家自己 +。檔案裡的每
# 一行也決定表上固定列出哪些時段（沒人也列，例如「日午」），其他時段有人登記
# 才出現。格式跟群組裡那張表一樣。
#
# 正本放在 NAS 的 data 目錄（容器的 /data），直接用檔案總管改就好、不用重新部署：
# 每次帶入新的一週都會重讀一次，所以改完之後，從下一個還沒帶入的週開始用新表；
# 已經帶入的週不會被改掉。data 目錄裡沒有這個檔時，退回映像裡附的那份。
DEFAULT_WEEKLY_DEFAULTS_PATH = "/data/weekly_defaults.txt"
BUNDLED_WEEKLY_DEFAULTS_PATH = str(Path(__file__).resolve().parent.parent / "weekly_defaults.txt")
_DEFAULTS_LINE = re.compile(r"^([一二三四五六日])([早午晚夜])(.*)$")
API_MAX_DAYS = 366

HELP_TEXT = """📋 SOGA 登記小幫手

【登記】
・+五晚 → 登記這週五晚上
・+一晚 三晚 → 一次登記好幾個
・+六晚 晚點到 → 後面的字當備註
・+下禮拜的六午 → 指定下週（下下禮拜＝下下週）
　已經登記過的再打一次就是改備註
　星期：一～日　時段：早 午 晚 夜
　已經過了的日子會登記到下週

【取消】
・-五晚 → 取消週五晚上
・取消 → 取消自己所有的登記

【查看】
・名單 → 看這週的表（週末會連下週一起列）

【第一次用】
・綁定 小魚 → 綁定你在 SOGA 的名字
　LINE 名字跟 SOGA 一樣的話會自動綁定
・綁定 → 查目前綁的是誰

📌 每個時段 {capacity} 人，第 {next} 個以後是備取
　前面有人取消，備取自動遞補
　第 {next} 個以後要寫備註，沒寫就是（備取）
📌 固定班底每週自動帶入，不能來請自己 -
📌 兩個群組共用同一張表
・說明 → 再看一次這段"""

# 不在 soga.db 正式名單裡的人，表上名字後面標這個。
NEWCOMER_NOTE = "新人"

ADMIN_HELP_TEXT = """🔧 管理員
・覆蓋 → 把整張表改成貼上的樣子
　第一行打「覆蓋」，下面貼整張表（可以先打「名單」複製來改）
　有日期那行就改那週，沒有就是這週
　表裡沒列的格子會清空；今天以前的不動
・班底 → 看目前每週自動帶入的班底表
・修改班底 → 換掉班底表
　第一行打「修改班底」，下面貼新的班底表（可以先打「班底」複製來改）
　從還沒帶入的週開始生效，已經開始的週請用「覆蓋」"""


def create_app(config: dict | None = None) -> Flask:
    # gunicorn 不會幫應用程式的 logger 設 INFO 等級的 handler，不設的話 INFO 全部被
    # 吞掉——群組 id、誰打了什麼指令，就是靠 INFO 那行「chat=...」查的。直接掛在
    # 套件自己的 logger 上：logging.basicConfig 在 gunicorn 底下是 no-op（2026-10-23
    # 部署後 INFO 一行都沒出來）。
    package_logger = logging.getLogger("soga_line_bot")
    if not package_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.INFO)
        package_logger.propagate = False
        # 另外寫一份到 data 目錄：每次部署都會重建容器，docker logs 跟著清空，
        # 出事時常常已經查不到（2026-10-23 一則「+一晚…日晚」就這樣查不到）。
        # 1MB × 5 份輪替，最多約 6MB，不會膨脹。
        log_dir = os.getenv("SOGA_LINE_BOT_LOG_DIR", "/data/logs")
        if os.path.isdir(os.path.dirname(log_dir)):
            os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(
                os.path.join(log_dir, "bot.log"), maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(handler.formatter)
            package_logger.addHandler(file_handler)
    app = Flask(__name__)
    app.config.update(
        LINE_CHANNEL_SECRET=os.getenv("LINE_CHANNEL_SECRET", ""),
        LINE_CHANNEL_ACCESS_TOKEN=os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        API_KEY=os.getenv("SOGA_LINE_BOT_API_KEY", ""),
        DB_PATH=os.getenv("SOGA_LINE_BOT_DB_PATH", "/data/line_bot.db"),
        SOGA_DB_PATH=os.getenv("SOGA_PLAYERS_DB_PATH", "/soga-data/soga.db"),
        WEEKLY_DEFAULTS_PATH=os.getenv("SOGA_WEEKLY_DEFAULTS_PATH", DEFAULT_WEEKLY_DEFAULTS_PATH),
        # 一個時段最多幾個人（一桌）。
        SLOT_CAPACITY=int(os.getenv("SOGA_SLOT_CAPACITY", "4")),
        # 只理會這些聊天室（逗號分隔的 groupId）。空的 = 全部都理。bot 被拉進
        # 陌生群組時不想讓它亂記，就把自己的群組 id 填進來（id 在 log 裡看得到）。
        # 管理員的 LINE userId（逗號分隔）。只有他們能「覆蓋」整張表。用 userId 而
        # 不是 SOGA 名字：名字是自己綁的，誰都能綁成管理員的名字。
        ADMIN_USER_IDS={s.strip() for s in os.getenv("LINE_ADMIN_USER_IDS", "").split(",") if s.strip()},
        # 只能看表的群組（逗號分隔的 groupId）：只回「名單」，其他指令都不理。
        READONLY_CHAT_IDS={s.strip() for s in os.getenv("LINE_READONLY_CHAT_IDS", "").split(",") if s.strip()},
        ALLOWED_CHAT_IDS={s.strip() for s in os.getenv("LINE_ALLOWED_CHAT_IDS", "").split(",") if s.strip()},
        NOW=lambda: datetime.now(TZ),
    )
    if config:
        app.config.update(config)

    store = SignupStore(app.config["DB_PATH"], app.config["SLOT_CAPACITY"])
    players = app.config.get("PLAYERS") or SogaPlayers(app.config["SOGA_DB_PATH"])
    line = app.config.get("LINE_CLIENT") or LineClient(app.config["LINE_CHANNEL_ACCESS_TOKEN"])
    # 改用 player id 綁定之前就綁好的人，補上 id。
    for bound_user, bound_name in store.bindings_without_player_id():
        player_id = players.player_id(bound_name)
        if player_id is not None:
            store.set_player_id(bound_user, player_id)

    def now() -> datetime:
        return app.config["NOW"]()

    def help_text() -> str:
        capacity = app.config["SLOT_CAPACITY"]
        return HELP_TEXT.format(capacity=capacity, next=capacity + 1)

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/callback")
    def callback():
        body = request.get_data()
        if not verify_signature(app.config["LINE_CHANNEL_SECRET"], body, request.headers.get("X-Line-Signature", "")):
            logger.warning("webhook 簽章不對，丟掉")
            abort(400)
        for event in (request.get_json(silent=True) or {}).get("events", []):
            # 一個事件出錯不能讓整包回非 200：LINE 會整包重送，前面已經處理過的
            # 事件就會再跑一次。
            try:
                handle_event(event)
            except Exception:
                logger.exception("處理 LINE 事件失敗：%s", event)
        return "OK"

    def handle_event(event: dict) -> None:
        source = event.get("source", {})
        chat_id = source.get("groupId") or source.get("roomId") or source.get("userId")
        if not chat_id:
            return
        allowed = app.config["ALLOWED_CHAT_IDS"]
        # 只管群組：一對一私訊 bot（很多人在私訊裡綁定）不受限制。
        readonly = chat_id in app.config["READONLY_CHAT_IDS"]
        if allowed and source.get("type") != "user" and chat_id not in allowed and not readonly:
            logger.info("不在允許清單的聊天室 %s，略過", chat_id)
            return
        remember_chat(source, chat_id)

        if event.get("type") == "join" and not readonly:
            line.reply_text(event["replyToken"], help_text())
            return
        message = event.get("message", {})
        if event.get("type") != "message" or message.get("type") != "text":
            return
        command = parser.parse(message.get("text", ""), now().date())
        if command is None:
            return
        logger.info("chat=%s user=%s 指令=%s", chat_id, source.get("userId"), command)
        if readonly and command.action != "board":
            logger.info("唯讀群組，只回名單，略過")
            return
        reply = run_command(command, source, chat_id)
        if reply:
            line.reply_text(event["replyToken"], reply)

    def remember_chat(source: dict, chat_id: str) -> None:
        # 群組名稱只在第一次看到這個群組時查一次，flask soga 那邊顯示用。
        if store.has_chat(chat_id):
            return
        name = line.group_name(chat_id) if source.get("type") == "group" else ""
        store.save_chat(chat_id, source.get("type", ""), name or "", now())
        logger.info("新的聊天室 %s（%s）", chat_id, name)

    def run_command(command: Command, source: dict, chat_id: str) -> str:
        if command.action == "help":
            text = help_text()
            if source.get("userId") in app.config["ADMIN_USER_IDS"]:
                text += "\n\n" + ADMIN_HELP_TEXT
            return text
        if command.action == "board":
            return board_reply(now().date())

        user_id = source.get("userId")
        if not user_id:
            return "讀不到你的 LINE 帳號，請先加 bot 好友再試一次"
        if command.action == "bind":
            return bind(command.name, source)
        if command.action == "overwrite":
            if user_id not in app.config["ADMIN_USER_IDS"]:
                return "只有管理員可以覆蓋整張表"
            return overwrite(command.body)
        if command.action in ("show_defaults", "set_defaults"):
            if user_id not in app.config["ADMIN_USER_IDS"]:
                return "只有管理員可以看或改班底表"
            if command.action == "show_defaults":
                return "📋 目前的班底表（每週自動帶入）\n\n" + defaults_text(weekly_defaults())
            return set_defaults(command.body, source)

        name = soga_name(source)
        if name is None:
            return "還不知道你在 SOGA 的名字，請先打「綁定 你的名字」，例如：綁定 小魚"
        current = now()
        if command.action == "cancel_all":
            this_week = week_start(current.date())
            touched = {this_week, this_week + timedelta(days=7)}
        else:
            touched = {week_start(s.day) for s in command.slots}
        # 先帶入班底再動：人數上限要把班底算進去，班底的 -五晚 也要有東西可以取消。
        defaults = weekly_defaults()
        for start in touched:
            seed(start, defaults)
        if command.action == "add":
            results: dict[str, list[str]] = {"added": [], "waitlisted": [], "noted": [], "exists": []}
            for slot in command.slots:
                results[store.add(chat_id, user_id, slot, current, command.body)].append(slot.key)
            note = f"（{command.body}）" if command.body else ""
            lines = []
            if results["added"]:
                lines.append(f"✅ {name} +{' '.join(results['added'])}{note}")
            if results["waitlisted"]:
                lines.append(f"🕒 {name} +{' '.join(results['waitlisted'])}{note or '（備取）'}")
            if results["noted"]:
                lines.append(f"📝 {name} {' '.join(results['noted'])} 備註改成{note}")
            if results["exists"]:
                lines.append(f"👌 {name} 已經登記過 {' '.join(results['exists'])}")
            head = "\n".join(lines)
            slots = command.slots
        elif command.action == "remove":
            changed, promoted = [], {}
            for slot in command.slots:
                removed, ids = store.remove(user_id, slot, current)
                if removed:
                    changed.append(slot)
                if ids:
                    promoted[slot] = ids
            head = f"🗑 {name} -{' '.join(s.key for s in changed)}" if changed else f"{name} 沒有登記這個時段"
            head += promotion_lines(promoted, source, chat_id)
            slots = command.slots
        else:  # cancel_all
            slots, promoted = store.remove_all(user_id, current.date(), current)
            if not slots:
                return f"{name} 目前沒有登記"
            # 跨週時同一個「三晚」可能有兩個，帶上日期才分得清。
            head = f"🗑 {name} 全部取消：{' '.join(f'{s.key}({s.day.month}/{s.day.day})' for s in slots)}"
            head += promotion_lines(promoted, source, chat_id)
        weeks = sorted({week_start(s.day) for s in slots})
        return head + "\n\n" + "\n\n".join(board_text(w) for w in weeks)

    def overwrite(body: str) -> str:
        """管理員把一整週改成貼上的表。

        週看表開頭那行「26/10/19 - 26/10/25」，沒有就是這週。貼上的每一格照貼的
        改（名字、順序、備註；不管人數上限）。今天以後、但貼的表裡沒有的格子清
        空——表就是「整張」的意思；今天以前的格子不動，因為群組裡的表本來就不列
        它們，複製來改的表裡自然沒有。
        """
        board = parser.parse_board(body)
        if not board:
            return "沒讀到表。格式：\n覆蓋\n26/10/19 - 26/10/25\n五晚 榮恩 兔子\n六午 賤綠"
        current = now()
        start = parser.board_week(body) or week_start(current.date())
        seed(start, weekly_defaults())
        listed = set()
        for weekday, period, people in board:
            slot = Slot(start + timedelta(days=weekday), period)
            listed.add(slot)
            store.set_slot(slot, people, current, players.canonical)
        end = start + timedelta(days=6)
        for slot in store.active_slots(max(start, current.date()), end) - listed:
            store.set_slot(slot, [], current, players.canonical)
        logger.info("管理員覆蓋 %s 那週：%s", start, board)
        return "✅ 已覆蓋整張表\n\n" + board_text(start)

    def promotion_lines(promoted: dict, source: dict, chat_id: str) -> str:
        """「⬆️ 小米 @VIKI 遞補 五晚」——備取的人被叫到時，在回覆裡講一聲並 @ 他。

        被遞補的人就在這個群組 → 直接在這則回覆 @ 他（回覆免費）。不在這個群組
        （他在另一群登記的）→ 回覆裡只寫名字，另外到他所在的群組推一則 @ 他（推播
        要扣額度，見 notify_in_own_group）。還沒綁定 LINE 的班底沒辦法 @，只寫名字。
        """
        lines = []
        for slot, user_ids in sorted(promoted.items(), key=lambda item: item[0].sort_key):
            known = {e.user_id: e for e in store.entries(slot.day, slot.day) if e.slot == slot}
            names = []
            for user_id in user_ids:
                entry = known.get(user_id)
                if entry is None:
                    continue
                name = players.current_name(entry.player_id) or players.canonical(entry.name)
                if user_id.startswith("name:"):
                    names.append(name)
                elif line.is_member(source.get("type", ""), chat_id, user_id):
                    names.append(f"{name} {mention(user_id)}")
                else:
                    names.append(name)
                    notify_in_own_group(user_id, name, slot, entry.chat_id, chat_id)
            if names:
                lines.append(f"⬆️ {' '.join(names)} 遞補 {slot.key}")
        return "".join("\n" + line for line in lines)

    def notify_in_own_group(user_id: str, name: str, slot: Slot, signed_up_in: str, current_chat: str) -> None:
        """到被遞補的人所在的群組推一則 @ 他。先找他當初登記的那一群，不在的話找
        其他可以登記的群組。推播按群組人數扣額度（免費方案每月 200 則），額度不夠就
        不推，只留 log。"""
        candidates = [signed_up_in] + sorted(app.config["ALLOWED_CHAT_IDS"])
        for group in dict.fromkeys(candidates):
            if group == current_chat or not group.startswith(("C", "R")):
                continue
            chat_type = "group" if group.startswith("C") else "room"
            if not line.is_member(chat_type, group, user_id):
                continue
            cost = line.member_count(chat_type, group) or 0
            left = line.quota_left()
            if left is not None and left < cost:
                logger.warning("推播額度剩 %s、這群要 %s，不推遞補通知（%s %s）", left, cost, name, slot.key)
                return
            text = f"⬆️ {name} {mention(user_id)} 遞補 {slot.key}（{slot.day.month}/{slot.day.day}）"
            if line.push_text(group, text):
                logger.info("到 %s 推播遞補通知：%s %s（約扣 %s 則）", group, name, slot.key, cost)
            return
        logger.info("找不到 %s 所在的群組，遞補只寫在回覆裡", name)

    def defaults_text(defaults: list) -> str:
        return "\n".join(f"{WEEKDAYS[weekday]}{period} {' '.join(names)}".rstrip() for weekday, period, names in defaults)

    def set_defaults(body: str, source: dict) -> str:
        """管理員換掉固定班底表（data 目錄那份 weekly_defaults.txt）。

        格式跟群組那張表一樣；括號備註（1415到、新人）不會寫進班底，舊名字換成
        目前的名字。舊的那份先備份到 data/weekly_defaults_history/。從還沒帶入的
        週開始生效——已經帶入的週不會被改，要改那週請用「覆蓋」。
        """
        board = parser.parse_board(body)
        if not board:
            return "沒讀到班底表。格式：\n修改班底\n一晚 日日 吉詳 小魚\n二晚 美顏 跳跳\n日午"
        defaults = [(weekday, period, [players.canonical(name) for name, _ in people]) for weekday, period, people in board]
        path = Path(app.config["WEEKLY_DEFAULTS_PATH"])
        stamp = now()
        if path.exists():
            history = path.parent / "weekly_defaults_history"
            history.mkdir(exist_ok=True)
            (history / f"{stamp:%Y%m%d-%H%M%S}.txt").write_text(path.read_text(encoding="utf-8-sig"), encoding="utf-8")
        # 原地覆寫而不是寫暫存檔再改名：改名會讓檔案變成容器的 root 擁有，之後用
        # 檔案總管就改不了了。
        with path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(defaults_text(defaults) + "\n")
        logger.info("管理員 %s 修改班底表：%s", source.get("userId"), defaults)

        # 告訴他新表從哪一週開始用：第一個還沒帶入的週。
        week = week_start(stamp.date())
        while store.seeded_slots(week) is not None:
            week += timedelta(days=7)
        note = f"從 {week:%m/%d} 那週開始用新班底"
        if week > week_start(stamp.date()) + timedelta(days=7):
            note += "（下週已經帶入了，要改下週請用「覆蓋」）"
        return f"✅ 班底表已更新，{note}\n\n" + defaults_text(defaults)

    def display_label(entry, known: set[str]) -> str:
        """表上的名字：用玩家 id 查目前的名字（改名後自動換），佔位或舊名字也換成
        目前的名字。不在 SOGA 正式名單（soga.db）裡的人照樣列，後面標「新人」；有
        備註就跟備註放在同一個括號裡，例如「歐羅拉（1415到、新人）」。名單讀不到
        時（known 是空的）不標，免得每個人都變新人。"""
        name = players.current_name(entry.player_id) or players.canonical(entry.name)
        notes = [part for part in entry.note.split("、") if part and part != NEWCOMER_NOTE]
        if known and name not in known:
            notes.append(NEWCOMER_NOTE)
        return f"{name}（{'、'.join(notes)}）" if notes else name

    def soga_name(source: dict) -> str | None:
        """這個 LINE 帳號在 SOGA 目前的名字。沒綁定過、但 LINE 名字剛好是某個 SOGA
        玩家（目前或以前）的名字時，自動幫他綁上。"""
        user_id = source["userId"]
        binding = store.binding_for(user_id)
        if binding:
            name, player_id = binding
            if player_id is None:
                # 綁定時還是新人，之後才同步進 soga.db：補上 id，之後改名才跟得上。
                player_id = players.player_id(name)
                if player_id is not None:
                    store.set_player_id(user_id, player_id)
                    logger.info("補上玩家 id %s → %s（#%s）", user_id, name, player_id)
            return players.current_name(player_id) or players.canonical(name)
        line_name = (line.display_name(source) or "").strip()
        player_id = players.player_id(line_name)
        if player_id is not None:
            name = players.current_name(player_id)
            if store.bind(user_id, name, line_name, now(), player_id, players.aliases(player_id)):
                logger.info("自動綁定 %s → %s（#%s）", user_id, name, player_id)
                return name
        return None

    def bind(name: str, source: dict) -> str:
        if not name:
            current = soga_name(source) if store.binding_for(source["userId"]) else None
            return f"你目前綁定的是：{current}" if current else "還沒綁定。請打「綁定 你的名字」，例如：綁定 小魚"
        line_name = line.display_name(source) or ""
        # 以玩家 id 綁定：打舊名字也對得到同一個人，之後他改名也不用重綁。
        player_id = players.player_id(name)
        current_name = players.current_name(player_id) or name
        if not store.bind(source["userId"], current_name, line_name, now(), player_id, players.aliases(player_id)):
            return f"「{current_name}」已經被另一個 LINE 帳號綁定了，有問題請找管理員"
        if player_id is None and players.names():
            return f"👌 已綁定：{name}\n（SOGA 名單裡還沒有這個名字；打錯的話再打一次「綁定 正確名字」）"
        renamed = f"（{name} 現在叫 {current_name}）" if current_name != name else ""
        return f"👌 已綁定：{line_name or '你'} → {current_name}{renamed}"

    def weekly_defaults() -> list[tuple[int, str, list[str]]]:
        return resolve_weekly_defaults(app.config["WEEKLY_DEFAULTS_PATH"])

    def seed(start: date, defaults: list) -> None:
        # 過去的週不補：查舊資料時不該憑空多出班底。
        if defaults and start >= week_start(now().date()) and store.seed_week(start, defaults, now(), players.canonical):
            logger.info("帶入 %s 那週的固定班底", start)

    def week_board(start: date, hide_past: bool = False, preview: bool = False) -> dict:
        """一週的表：固定時段一定列出，其他時段有人登記才列。

        hide_past：群組裡回的表只列今天以後（含今天）的時段，已經過去的那幾天
        不列；給 flask soga 的 API 則整週都給，要不要藏由前端決定。

        preview：API 用。還沒帶入班底的週只「預覽」班底、不寫進資料庫——不然前端
        每查一次就把下週提早帶入，之後改班底表就吃不到了。"""
        defaults = weekly_defaults()
        if not preview:
            seed(start, defaults)
        end = start + timedelta(days=6)
        names: dict[Slot, list[str]] = {}
        fixed = store.seeded_slots(start)
        unseeded = fixed is None
        if unseeded:
            fixed = [(weekday, period) for weekday, period, _ in defaults]
        for weekday, period in fixed:
            names[Slot(start + timedelta(days=weekday), period)] = []
        if unseeded and preview and start >= week_start(now().date()):
            for weekday, period, roster in defaults:
                names[Slot(start + timedelta(days=weekday), period)].extend(roster)
        known = players.names()
        for entry in store.entries(start, end):
            names.setdefault(entry.slot, []).append(display_label(entry, known))
        slots = sorted(names, key=lambda s: s.sort_key)
        if hide_past:
            today = now().date()
            slots = [s for s in slots if s.day >= today]
        lines = [f"{start:%y/%m/%d} - {end:%y/%m/%d}"]
        lines += [f"{s.key} {' '.join(names[s])}".rstrip() for s in slots]
        return {
            "weekStart": start.isoformat(),
            "weekEnd": end.isoformat(),
            "text": "\n".join(lines),
            "slots": [
                {"slot": s.key, "date": s.day.isoformat(), "period": s.period, "names": names[s]} for s in slots
            ],
        }

    def board_text(start: date) -> str:
        return week_board(start, hide_past=True)["text"]

    def board_reply(today: date) -> str:
        """這週的表；週末（大家開始排下週）或下週已經有人登記的話，下週一起列。"""
        this_week = week_start(today)
        next_week = this_week + timedelta(days=7)
        texts = [board_text(this_week)]
        if today.weekday() >= 5 or store.entries(next_week, next_week + timedelta(days=6)):
            texts.append(board_text(next_week))
        return "\n\n".join(texts)

    @app.get("/api/signups")
    def api_signups():
        """給 flask soga 讀的登記資料。

        Query:
            from: YYYY-MM-DD，預設這週一。
            to: YYYY-MM-DD（含），預設 from + 13 天（兩週），最多跨 366 天。
        Header:
            X-Api-Key: 要等於 SOGA_LINE_BOT_API_KEY。

        Returns:
            signups: 每一筆登記；weeks: 範圍內每一週的表（跟群組裡回的同一張，
            text 是整張表的文字，slots 是拆開的資料）。
        """
        expected = app.config["API_KEY"]
        if not expected:
            return jsonify({"status": 503, "message": "SOGA_LINE_BOT_API_KEY 沒設定，查詢 API 關閉中"}), 503
        if request.headers.get("X-Api-Key", "") != expected:
            return jsonify({"status": 401, "message": "Unauthorized"}), 401
        try:
            start = date.fromisoformat(request.args["from"]) if request.args.get("from") else week_start(now().date())
            end = date.fromisoformat(request.args["to"]) if request.args.get("to") else start + timedelta(days=13)
        except ValueError:
            return jsonify({"status": 400, "message": "from/to 要是 YYYY-MM-DD"}), 400
        if end < start or (end - start).days > API_MAX_DAYS:
            return jsonify({"status": 400, "message": f"to 要在 from 之後、且不超過 {API_MAX_DAYS} 天"}), 400

        chat_names = store.chat_names()
        weeks = []
        monday = week_start(start)
        while monday <= end:
            weeks.append(week_board(monday, preview=True))
            monday += timedelta(days=7)
        return jsonify(
            {
                "status": 200,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "signups": [e.to_json(chat_names.get(e.chat_id, "")) for e in store.entries(start, end)],
                "weeks": weeks,
            }
        )

    return app


def resolve_weekly_defaults(path: str) -> list[tuple[int, str, list[str]]]:
    """讀 data 目錄那份班底表；沒有的話讀映像裡附的那份。"""
    return load_weekly_defaults(path if Path(path).exists() else BUNDLED_WEEKLY_DEFAULTS_PATH)


def load_weekly_defaults(path: str) -> list[tuple[int, str, list[str]]]:
    """讀固定班底檔，回傳 [(星期幾 0=一, 時段, [名字...]), ...]，順序照檔案。"""
    try:
        # utf-8-sig：記事本存檔可能帶 BOM，不吃掉的話第一行會對不上格式。
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        logger.warning("讀不到固定班底檔 %s：%s，每週從空表開始", path, exc)
        return []
    result = []
    for line in text.splitlines():
        match = _DEFAULTS_LINE.match(line.strip())
        if match:
            result.append((WEEKDAYS.index(match.group(1)), match.group(2), match.group(3).split()))
        elif line.strip():
            logger.warning("固定班底檔看不懂這一行，略過：%s", line)
    return result
