import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime

from soga_line_bot.app import create_app
from soga_line_bot.parser import TZ
from soga_line_bot.players import SogaPlayers

SECRET = "test-secret"
API_KEY = "test-key"
GROUP_A = "Cgroup-a"
GROUP_B = "Cgroup-b"
# 2026-10-23 是星期五。
NOW = datetime(2026, 10, 23, 15, 0, tzinfo=TZ)

# 固定時段（班底表沒有名字的版本）。
FIXED_SLOTS = """一晚
二晚
三晚
四晚
五晚
六午
六晚
日午
日晚"""
# 今天週五：群組裡的表只列五晚以後。
EMPTY_WEEK = """26/10/19 - 26/10/25
五晚
六午
六晚
日午
日晚"""


class FakeLine:
    def __init__(self):
        self.replies = []
        # U1 的 LINE 名字剛好就是 SOGA 名字；U2 不是。
        self.names = {"U1": "小魚", "U2": "阿華華", "U3": "隨便"}

    def reply_text(self, reply_token, text):
        self.replies.append(text)

    def display_name(self, source):
        return self.names.get(source.get("userId"))

    def group_name(self, group_id):
        return {"Cgroup-a": "A 群", "Cgroup-b": "B 群"}.get(group_id)


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        soga_db = os.path.join(self.tmp.name, "soga.db")
        conn = sqlite3.connect(soga_db)
        conn.execute(
            "CREATE TABLE history_player_info (id INTEGER PRIMARY KEY, current_name TEXT, history_name TEXT)"
        )
        # 正式成員：班底全部、加上大樹。歐羅拉、新朋友不在裡面（新人）。
        members = "日日 吉詳 小魚 美顏 跳跳 安妮 胖弟 阿希 阿凱 居然 郝灣 阿陞 月月 邊勸 榮恩 兔子 賤綠 熠熠 玖玖 嘉嘉 小米 鴨鴨 糖果 大樹"
        conn.executemany(
            "INSERT INTO history_player_info (current_name, history_name) VALUES (?, ?)",
            [(n, json.dumps([n], ensure_ascii=False)) for n in members.split()],
        )
        # 改過名的玩家：榮恩以前叫榮五。
        conn.execute("UPDATE history_player_info SET history_name=? WHERE current_name='榮恩'", ('["榮五", "榮恩"]',))
        conn.commit()
        self.soga_db_path = soga_db
        conn.close()

        self.soga_db = soga_db
        self.line = FakeLine()
        # 預設沒有班底，只有固定時段——大部分測試從空表開始比較好讀。
        self.make_app(FIXED_SLOTS)

    def make_app(self, weekly_defaults):
        defaults_path = os.path.join(self.tmp.name, "weekly_defaults.txt")
        with open(defaults_path, "w", encoding="utf-8") as f:
            f.write(weekly_defaults)
        self.app = create_app(
            {
                "LINE_CHANNEL_SECRET": SECRET,
                "API_KEY": API_KEY,
                "DB_PATH": os.path.join(self.tmp.name, "bot.db"),
                "PLAYERS": SogaPlayers(self.soga_db),
                "WEEKLY_DEFAULTS_PATH": defaults_path,
                "ALLOWED_CHAT_IDS": set(),
                "LINE_CLIENT": self.line,
                "NOW": lambda: NOW,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def post_events(self, events, secret=SECRET):
        body = json.dumps({"destination": "Ubot", "events": events}).encode("utf-8")
        signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
        return self.client.post(
            "/callback", data=body, headers={"X-Line-Signature": signature, "Content-Type": "application/json"}
        )

    def say(self, text, user="U1", chat=GROUP_A):
        before = len(self.line.replies)
        event = {
            "type": "message",
            "replyToken": "r",
            "source": {"type": "group", "groupId": chat, "userId": user},
            "message": {"type": "text", "id": "1", "text": text},
        }
        self.assertEqual(self.post_events([event]).status_code, 200)
        return self.line.replies[-1] if len(self.line.replies) > before else None

    def api(self, query="", key=API_KEY):
        return self.client.get(f"/api/signups{query}", headers={"X-Api-Key": key})


class AppTest(BotTestCase):
    def test_rejects_bad_signature(self):
        self.assertEqual(self.post_events([], secret="wrong").status_code, 400)

    def test_empty_board(self):
        self.assertEqual(self.say("名單"), EMPTY_WEEK)

    def test_api_keeps_whole_week(self):
        week = self.api().get_json()["weeks"][0]
        self.assertEqual(week["text"], "26/10/19 - 26/10/25\n" + FIXED_SLOTS)

    def test_auto_bind_and_board_shared_across_groups(self):
        reply = self.say("+五晚")
        self.assertTrue(reply.startswith("✅ 小魚 +五晚\n\n26/10/19 - 26/10/25\n"))
        self.assertIn("\n五晚 小魚\n", reply)

        self.say("綁定 日日", user="U2", chat=GROUP_B)
        reply = self.say("+五晚 六午", user="U2", chat=GROUP_B)
        self.assertIn("✅ 日日 +五晚 六午", reply)
        self.assertIn("\n五晚 小魚 日日\n", reply)
        self.assertIn("\n六午 日日\n", reply)

        # A 群看到的是同一張表。
        self.assertIn("\n五晚 小魚 日日\n", self.say("名單", chat=GROUP_A))

    def test_duplicate_and_remove(self):
        self.say("+五晚")
        self.assertIn("👌 小魚 已經登記過 五晚", self.say("+五晚", chat=GROUP_B))
        reply = self.say("-五晚")
        self.assertIn("🗑 小魚 -五晚", reply)
        self.assertIn("\n五晚\n", reply)
        self.assertIn("沒有登記這個時段", self.say("-五晚"))

    def test_cancel_all(self):
        self.assertIn("小魚 目前沒有登記", self.say("取消"))
        self.say("+五晚 六午 一晚")
        self.say("綁定 日日", user="U2")
        self.say("+五晚", user="U2", chat=GROUP_B)
        reply = self.say("全部取消")
        self.assertTrue(reply.startswith("🗑 小魚 全部取消：五晚(10/23) 六午(10/24) 一晚(10/26)\n\n26/10/19 - 26/10/25\n"))
        self.assertIn("26/10/26 - 26/11/01", reply)
        # 只取消自己的，別人的還在。
        self.assertIn("\n五晚 日日\n", reply)
        self.assertIn("\n六午\n", reply)

    def test_slot_capacity(self):
        users = {"U1": "小魚", "U2": "日日", "U3": "阿凱", "U4": "吉詳", "U5": "安妮"}
        self.line.names.update(users)
        for user in ["U2", "U3", "U4", "U5"]:
            self.say(f"綁定 {users[user]}", user=user)
        for user in ["U1", "U2", "U3", "U4"]:
            self.assertIn("✅", self.say("+五晚", user=user, chat=GROUP_A if user < "U3" else GROUP_B))
        reply = self.say("+五晚 六午", user="U5")
        self.assertIn("✅ 安妮 +六午", reply)
        self.assertIn("❌ 五晚 已經滿 4 人了", reply)
        self.assertIn("\n五晚 小魚 日日 阿凱 吉詳\n", reply)
        # 有人取消就空出位子。
        self.say("-五晚", user="U3")
        self.assertIn("✅ 安妮 +五晚", self.say("+五晚", user="U5"))

    def test_next_week_and_extra_slot(self):
        reply = self.say("+一晚 三午")
        self.assertIn("26/10/26 - 26/11/01", reply)
        self.assertIn("\n一晚 小魚\n二晚\n三午 小魚\n三晚\n", reply)
        # 這週沒人，但下週有人 → 名單兩週都列。
        board = self.say("名單")
        self.assertTrue(board.startswith(EMPTY_WEEK + "\n\n26/10/26 - 26/11/01"))

    def test_unbound_user_must_bind(self):
        self.assertIn("請先打「綁定 你的名字」", self.say("+五晚", user="U2"))
        self.assertIn("還沒綁定", self.say("綁定", user="U2"))
        self.assertIn("已綁定：阿華華 → 阿凱", self.say("綁定 阿凱", user="U2"))
        self.assertIn("你目前綁定的是：阿凱", self.say("綁定", user="U2"))

    def test_bind_conflict_and_unknown_name(self):
        self.say("+五晚")  # U1 自動綁定成小魚
        self.assertIn("已經被另一個 LINE 帳號綁定", self.say("綁定 小魚", user="U2"))
        self.assertIn("SOGA 名單裡還沒有這個名字", self.say("綁定 新朋友", user="U3"))

    def test_rebind_renames_existing_signups(self):
        self.say("綁定 阿凱", user="U2")
        self.say("+五晚", user="U2")
        self.say("綁定 日日", user="U2")
        self.assertIn("\n五晚 日日\n", self.say("名單"))

    def test_ignores_normal_chat(self):
        self.assertIsNone(self.say("五晚有人嗎"))

    def test_join_sends_help(self):
        self.post_events([{"type": "join", "replyToken": "r", "source": {"type": "group", "groupId": GROUP_A}}])
        self.assertIn("SOGA 登記小幫手", self.line.replies[-1])

    def test_allowed_chats(self):
        self.app.config["ALLOWED_CHAT_IDS"] = {"Cother"}
        self.assertIsNone(self.say("+五晚"))

    def test_api(self):
        self.assertEqual(self.api(key="nope").status_code, 401)
        self.say("+五晚")
        self.say("+一晚")

        body = self.api().get_json()
        self.assertEqual((body["from"], body["to"]), ("2026-10-19", "2026-11-01"))
        self.assertEqual([(s["slot"], s["name"], s["chatName"]) for s in body["signups"]],
                         [("五晚", "小魚", "A 群"), ("一晚", "小魚", "A 群")])
        self.assertEqual([w["weekStart"] for w in body["weeks"]], ["2026-10-19", "2026-10-26"])
        self.assertIn("五晚 小魚", body["weeks"][0]["text"])
        friday = next(s for s in body["weeks"][0]["slots"] if s["slot"] == "五晚")
        self.assertEqual(friday, {"slot": "五晚", "date": "2026-10-23", "period": "晚", "names": ["小魚"]})

        self.assertEqual(self.api("?from=bad").status_code, 400)
        self.assertEqual(self.api("?from=2026-11-01&to=2026-10-01").status_code, 400)

    def test_api_disabled_without_key(self):
        self.app.config["API_KEY"] = ""
        self.assertEqual(self.api().status_code, 503)


if __name__ == "__main__":
    unittest.main()


ROSTER = """一晚 日日 吉詳 小魚
二晚 美顏 跳跳 安妮 胖弟
三晚 阿希 阿凱 居然 郝灣
四晚 阿陞 月月
五晚 邊勸 榮恩 兔子
六午 賤綠 熠熠 玖玖
六晚 嘉嘉
日午
日晚 小米 鴨鴨 糖果
"""


ROSTER_FROM_FRIDAY = "\n".join(ROSTER.splitlines()[4:])


class WeeklyDefaultsTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.make_app(ROSTER)

    def test_each_week_starts_with_roster(self):
        board = self.say("名單")
        # 今天週五，一晚～四晚已經過了不列。
        self.assertEqual(board, "26/10/19 - 26/10/25\n" + ROSTER_FROM_FRIDAY)
        self.say("綁定 新朋友", user="U3")
        reply = self.say("+四晚", user="U3")
        # 下週也帶入了班底，新朋友排在後面。
        self.assertIn("26/10/26 - 26/11/01\n一晚 日日 吉詳 小魚\n", reply)
        self.assertIn("\n四晚 阿陞 月月 新朋友（新人）\n", reply)

    def test_roster_counts_toward_capacity(self):
        self.say("綁定 新朋友", user="U3")
        self.assertIn("❌ 二晚 已經滿 4 人了", self.say("+二晚", user="U3"))

    def test_roster_member_cancels_after_binding(self):
        # U1 的 LINE 名字是「小魚」，第一次打指令就自動綁定，佔位登記換成他的帳號。
        reply = self.say("-一晚")
        self.assertIn("🗑 小魚 -一晚", reply)
        self.assertIn("\n一晚 日日 吉詳\n", reply)
        # 那週已經帶入過了，不會再把他補回去。
        self.assertIn("\n一晚 日日 吉詳\n", self.say("名單"))

    def test_roster_member_registering_same_slot_is_not_duplicated(self):
        self.assertIn("👌 小魚 已經登記過 一晚", self.say("+一晚"))
        self.assertIn("\n一晚 日日 吉詳 小魚\n", self.say("名單"))

    def test_cancel_all_removes_roster_slots(self):
        self.say("綁定 阿凱", user="U2")
        reply = self.say("取消", user="U2")
        self.assertIn("🗑 阿凱 全部取消：三晚(10/28)", reply)
        self.assertIn("\n三晚 阿希 居然 郝灣\n", reply)

    def test_edited_roster_applies_from_next_unseeded_week(self):
        self.say("名單")  # 這週帶入舊表
        with open(self.app.config["WEEKLY_DEFAULTS_PATH"], "w", encoding="utf-8") as f:
            f.write("一晚 阿凱\n三午\n日晚 小米\n")
        board = self.say("名單")
        # 這週不變，連固定列出的時段都照舊。
        self.assertEqual(board, "26/10/19 - 26/10/25\n" + ROSTER_FROM_FRIDAY)
        # 下週用新表：一晚只有阿凱、多了固定的三午、二晚不再固定列出。
        reply = self.say("+二晚")
        self.assertIn("26/10/26 - 26/11/01\n一晚 阿凱\n二晚 小魚\n三午\n日晚 小米", reply)

    def test_api_previews_without_seeding(self):
        body = self.api().get_json()
        # 兩週都看得到班底……
        self.assertIn("一晚 日日 吉詳 小魚", body["weeks"][1]["text"])
        # ……但沒有寫進資料庫：改了班底表之後，下週照新表帶入。
        self.assertEqual(body["signups"], [])
        with open(self.app.config["WEEKLY_DEFAULTS_PATH"], "w", encoding="utf-8") as f:
            f.write("一晚 阿凱\n")
        self.assertIn("26/10/26 - 26/11/01\n一晚 阿凱 小魚", self.say("+一晚"))


class SetBoardTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.make_app(ROSTER)

    def test_set_slots_with_notes(self):
        from soga_line_bot.set_board import parse_board
        from soga_line_bot.store import SignupStore
        from soga_line_bot.parser import Slot

        self.say("名單")  # 先帶入班底
        store = SignupStore(self.app.config["DB_PATH"])
        board = parse_board("26/10/19 - 26/10/25\n五晚 榮恩 兔子 大樹 安妮\n日午 玖玖 兔子（1415到）\n")
        self.assertEqual(board[1], (6, "午", [("玖玖", ""), ("兔子", "1415到")]))
        for weekday, period, people in board:
            store.set_slot(Slot(datetime(2026, 10, 19 + weekday).date(), period), people, NOW)
        text = self.say("名單")
        self.assertIn("\n五晚 榮恩 兔子 大樹 安妮\n", text)
        self.assertIn("\n日午 玖玖 兔子（1415到）\n", text)
        # 沒列到的時段不動。
        self.assertIn("\n六晚 嘉嘉\n", text)
        # 已經滿 4 人。
        self.say("綁定 新朋友", user="U3")
        self.assertIn("❌ 五晚 已經滿 4 人了", self.say("+五晚", user="U3"))


class OverwriteTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.make_app(ROSTER)
        self.app.config["ADMIN_USER_IDS"] = {"U1"}

    def test_admin_overwrites_board(self):
        self.say("綁定 新朋友", user="U3")
        self.say("+五夜", user="U3")  # 今天以後、表裡沒列的格子 → 清空
        reply = self.say(
            "覆蓋\n26/10/19 - 26/10/25\n五晚 榮恩 兔子 大樹 安妮 阿凱\n六午 賤綠\n六晚\n日午 玖玖 兔子（1415到）\n日晚 小米"
        )
        self.assertEqual(
            reply,
            "✅ 已覆蓋整張表\n\n26/10/19 - 26/10/25\n五晚 榮恩 兔子 大樹 安妮 阿凱\n六午 賤綠\n六晚\n"
            "日午 玖玖 兔子（1415到）\n日晚 小米",
        )
        # 今天以前的格子不動（API 整週都給）。
        week = self.api().get_json()["weeks"][0]["text"]
        self.assertIn("\n一晚 日日 吉詳 小魚\n", week)

    def test_overwrite_other_week_by_header(self):
        reply = self.say("覆蓋\n26/10/26 - 26/11/01\n一晚 阿凱")
        self.assertTrue(reply.startswith("✅ 已覆蓋整張表\n\n26/10/26 - 26/11/01\n一晚 阿凱\n"))
        # 下週沒列的班底格子清空。
        self.assertIn("\n二晚\n", reply)

    def test_non_admin_cannot_overwrite(self):
        self.assertEqual(self.say("覆蓋\n五晚 阿凱", user="U2"), "只有管理員可以覆蓋整張表")
        self.assertIn("\n五晚 邊勸 榮恩 兔子\n", self.say("名單"))

    def test_admin_help(self):
        self.assertIn("🔧 管理員", self.say("說明"))
        self.assertNotIn("🔧 管理員", self.say("說明", user="U2"))


class NewcomerTest(BotTestCase):
    def test_newcomer_marked(self):
        self.app.config["ADMIN_USER_IDS"] = {"U1"}
        # soga.db 裡只有 小魚、日日、阿凱。
        reply = self.say("覆蓋\n五晚 小魚 歐羅拉 阿凱（1415到）\n六午 歐羅拉（新人）\n六晚 日日（晚到）")
        self.assertIn("\n五晚 小魚 歐羅拉（新人） 阿凱（1415到）\n", reply)
        # 管理員把「（新人）」一起貼回來也不會重複標。
        self.assertIn("\n六午 歐羅拉（新人）\n", reply)
        self.assertIn("\n六晚 日日（晚到）\n", reply)
        self.say("綁定 新朋友", user="U3")
        self.assertIn("\n日晚 新朋友（新人）", self.say("+日晚", user="U3"))


class AllowedChatsTest(BotTestCase):
    def test_only_allowed_groups_but_private_chat_ok(self):
        self.app.config["ALLOWED_CHAT_IDS"] = {GROUP_A}
        self.assertIsNone(self.say("+五晚", chat=GROUP_B))
        self.assertIn("✅ 小魚 +五晚", self.say("+五晚", chat=GROUP_A))
        before = len(self.line.replies)
        self.post_events([{
            "type": "message", "replyToken": "r",
            "source": {"type": "user", "userId": "U1"},
            "message": {"type": "text", "id": "1", "text": "綁定"},
        }])
        self.assertIn("你目前綁定的是：小魚", self.line.replies[before])


class RenameTest(BotTestCase):
    def rename(self, old, new):
        conn = sqlite3.connect(self.soga_db_path)
        row = conn.execute("SELECT id, history_name FROM history_player_info WHERE current_name=?", (old,)).fetchone()
        history = json.loads(row[1]) + [new]
        conn.execute("UPDATE history_player_info SET current_name=?, history_name=? WHERE id=?",
                     (new, json.dumps(history, ensure_ascii=False), row[0]))
        conn.commit()
        conn.close()
        self.app.config["PLAYERS"]._loaded_at = 0  # 讓名單快取重讀

    def test_bind_by_old_name_shows_current_name(self):
        self.assertIn("已綁定：阿華華 → 榮恩（榮五 現在叫 榮恩）", self.say("綁定 榮五", user="U2"))
        self.assertIn("✅ 榮恩 +五晚", self.say("+五晚", user="U2"))

    def test_rename_after_binding_follows_player_id(self):
        self.say("綁定 阿凱", user="U2")
        self.say("+五晚", user="U2")
        self.rename("阿凱", "凱凱")
        board = self.say("名單")
        self.assertIn("\n五晚 凱凱\n", board)
        self.assertIn("✅ 凱凱 +六午", self.say("+六午", user="U2"))
        # 改名後別人不能用新名字或舊名字把他搶走。
        self.assertIn("已經被另一個 LINE 帳號綁定", self.say("綁定 凱凱", user="U3"))
        self.assertIn("已經被另一個 LINE 帳號綁定", self.say("綁定 阿凱", user="U3"))

    def test_roster_with_old_name(self):
        self.make_app("五晚 榮五 兔子")
        self.assertIn("\n五晚 榮恩 兔子", self.say("名單"))
        # 榮恩綁定後，班底表裡寫成舊名字的位子也歸給他，可以自己取消。
        self.say("綁定 榮恩", user="U2")
        self.assertIn("🗑 榮恩 -五晚", self.say("-五晚", user="U2"))


class ReadonlyChatTest(BotTestCase):
    def test_readonly_group_only_answers_board(self):
        self.app.config["ALLOWED_CHAT_IDS"] = {GROUP_A}
        self.app.config["READONLY_CHAT_IDS"] = {GROUP_B}
        self.say("+五晚", chat=GROUP_A)
        self.assertIn("\n五晚 小魚\n", self.say("名單", chat=GROUP_B))
        self.assertIsNone(self.say("+六午", chat=GROUP_B))
        self.assertIsNone(self.say("說明", chat=GROUP_B))
        self.assertIsNone(self.say("綁定 日日", user="U2", chat=GROUP_B))
        before = len(self.line.replies)
        self.post_events([{"type": "join", "replyToken": "r", "source": {"type": "group", "groupId": GROUP_B}}])
        self.assertEqual(len(self.line.replies), before)


class DefaultsCommandTest(BotTestCase):
    def setUp(self):
        super().setUp()
        self.make_app(ROSTER)
        self.app.config["ADMIN_USER_IDS"] = {"U1"}

    def test_admin_views_and_replaces_roster(self):
        self.assertEqual(self.say("班底"), "📋 目前的班底表（每週自動帶入）\n\n" + ROSTER.rstrip())
        self.say("名單")  # 這週已經帶入
        reply = self.say("修改DEFAULT\n一晚 榮五 吉詳（1415到）\n三午\n日晚 歐羅拉（新人）")
        self.assertEqual(reply, "✅ 班底表已更新，從 10/26 那週開始用新班底\n\n一晚 榮恩 吉詳\n三午\n日晚 歐羅拉")
        path = self.app.config["WEEKLY_DEFAULTS_PATH"]
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "一晚 榮恩 吉詳\n三午\n日晚 歐羅拉\n")
        history = os.listdir(os.path.join(os.path.dirname(path), "weekly_defaults_history"))
        self.assertEqual(len(history), 1)
        # 這週不變；下週用新班底。
        self.assertIn("\n五晚 邊勸 榮恩 兔子\n", self.say("名單"))
        self.assertIn("26/10/26 - 26/11/01\n一晚 榮恩 吉詳 小魚\n三午\n", self.say("+一晚"))

    def test_warns_when_next_week_already_seeded(self):
        self.say("名單")  # 這週已帶入
        self.say("+一晚")  # 下週也已帶入
        reply = self.say("修改班底\n一晚 阿凱")
        self.assertIn("從 11/02 那週開始用新班底（下週已經帶入了，要改下週請用「覆蓋」）", reply)

    def test_non_admin(self):
        self.assertEqual(self.say("班底", user="U2"), "只有管理員可以看或改班底表")
        self.assertEqual(self.say("修改班底\n一晚 阿凱", user="U2"), "只有管理員可以看或改班底表")


class MemberNoteTest(BotTestCase):
    def test_add_with_note_and_update(self):
        reply = self.say("+六晚 晚點到")
        self.assertIn("✅ 小魚 +六晚（晚點到）", reply)
        self.assertIn("\n六晚 小魚（晚點到）\n", reply)
        reply = self.say("+六晚 (1415到)")
        self.assertIn("📝 小魚 六晚 備註改成（1415到）", reply)
        self.assertIn("\n六晚 小魚（1415到）\n", reply)
        # 不帶備註再打一次：不動備註。
        self.assertIn("👌 小魚 已經登記過 六晚", self.say("+六晚"))
        self.assertIn("\n六晚 小魚（1415到）\n", self.say("名單"))

    def test_long_note_truncated(self):
        reply = self.say("+六晚 " + "很長" * 20)
        self.assertIn("小魚（" + ("很長" * 20)[:15] + "）", reply)
