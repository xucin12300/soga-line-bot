"""登記資料存在 NAS 上的一個 SQLite 檔（容器的 /data volume）。

- signups：誰登記了哪一天的哪個時段。兩個群組共用同一張表，所以唯一鍵不含
  聊天室——同一個人從兩邊各打一次「+五晚」只算一次。取消不刪資料，只蓋上
  cancelled_at，之後要查「誰報了又取消」還查得到。
- bindings：LINE 帳號 → SOGA 名字。表上顯示的是 SOGA 名字，改綁定後舊的登記
  也跟著換名字（顯示時才 join，登記本身只記 user_id）。
- chats：bot 待過的群組與群組名稱。
- seeded_weeks：哪幾週已經帶入過固定班底（weekly_defaults.txt）。每週只帶一次，
  之後班底自己 -五晚 取消不會再被補回去，班底檔改了也不會回頭改這週。

固定班底不一定綁定過 LINE，所以他們的登記 user_id 先記成 "name:日日" 這種佔位
id；等那個人綁定（或 LINE 名字自動對上）時，再把佔位 id 換成他的 LINE 帳號，
他就能自己取消。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .parser import Slot

DEFAULT_CHAT_ID = "default"
NAME_PREFIX = "name:"
MANUAL_CHAT_ID = "manual"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,          -- 從哪個群組登記的（groupId / roomId / userId）
    user_id TEXT NOT NULL,
    slot_date TEXT NOT NULL,        -- YYYY-MM-DD（台北時間）
    period TEXT NOT NULL,           -- 早 / 午 / 晚 / 夜
    note TEXT NOT NULL DEFAULT '',  -- 表上名字後面的括號，例如「1415到」
    created_at TEXT NOT NULL,
    cancelled_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS signups_active_unique
    ON signups (user_id, slot_date, period) WHERE cancelled_at IS NULL;
CREATE INDEX IF NOT EXISTS signups_date ON signups (slot_date);

CREATE TABLE IF NOT EXISTS bindings (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,      -- 綁定當時的 SOGA 名字（改名後以 player_id 為準）
    player_id INTEGER,              -- history_player_info.id；不在 soga.db 的新人是 NULL
    line_name TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seeded_weeks (
    week_start TEXT PRIMARY KEY,    -- 那週的週一 YYYY-MM-DD
    slots TEXT NOT NULL DEFAULT '', -- 帶入當時班底表的固定時段，例如「一晚,二晚,日午」
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    chat_type TEXT NOT NULL,        -- group / room / user
    name TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Entry:
    id: int
    chat_id: str
    user_id: str
    name: str
    slot: Slot
    created_at: str
    note: str = ""
    player_id: int | None = None

    @property
    def label(self) -> str:
        """表上的寫法：名字，有備註就接在括號裡，例如「兔子（1415到）」。"""
        return f"{self.name}（{self.note}）" if self.note else self.name

    def to_json(self, chat_name: str = "") -> dict:
        return {
            "id": self.id,
            "chatId": self.chat_id,
            "chatName": chat_name,
            "userId": self.user_id,
            "name": self.name,
            "date": self.slot.day.isoformat(),
            "period": self.slot.period,
            "slot": self.slot.key,
            "note": self.note,
            "createdAt": self.created_at,
        }


class SignupStore:
    def __init__(self, path: str):
        self.path = path
        # 一個 gunicorn worker 裡有好幾條 thread；「先查有沒有、沒有才新增」這種
        # 兩步驟要整段鎖住才不會重複。
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            # 2026-10-23 加的欄位；更早建的資料庫補上。
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(signups)")}
            if "note" not in columns:
                conn.execute("ALTER TABLE signups ADD COLUMN note TEXT NOT NULL DEFAULT ''")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(bindings)")}
            if "player_id" not in columns:
                conn.execute("ALTER TABLE bindings ADD COLUMN player_id INTEGER")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, chat_id: str, user_id: str, slot: Slot, capacity: int, now: datetime, note: str = "") -> str:
        """登記一個時段。回傳 "added"、"noted"（已經登記過、這次只更新備註）、
        "exists"（已經登記過）或 "full"（滿了）。人數檢查跟新增在同一把鎖裡，兩群
        同時搶最後一個位子也只會進去一個。"""
        with self._lock, self._connect() as conn:
            taken = [
                row["user_id"]
                for row in conn.execute(
                    "SELECT user_id FROM signups WHERE slot_date=? AND period=? AND cancelled_at IS NULL",
                    (slot.day.isoformat(), slot.period),
                )
            ]
            if user_id in taken:
                if not note:
                    return "exists"
                conn.execute(
                    "UPDATE signups SET note=? WHERE user_id=? AND slot_date=? AND period=? AND cancelled_at IS NULL",
                    (note, user_id, slot.day.isoformat(), slot.period),
                )
                return "noted"
            if len(taken) >= capacity:
                return "full"
            conn.execute(
                "INSERT INTO signups (chat_id, user_id, slot_date, period, note, created_at) VALUES (?,?,?,?,?,?)",
                (chat_id, user_id, slot.day.isoformat(), slot.period, note, now.isoformat(timespec="seconds")),
            )
            return "added"

    def remove(self, user_id: str, slot: Slot, now: datetime) -> bool:
        """取消一個時段，回傳是否真的有取消到。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE signups SET cancelled_at=? WHERE user_id=? AND slot_date=? AND period=? AND cancelled_at IS NULL",
                (now.isoformat(timespec="seconds"), user_id, slot.day.isoformat(), slot.period),
            )
            return cursor.rowcount > 0

    def remove_all(self, user_id: str, today: date, now: datetime) -> list[Slot]:
        """取消這個人今天起所有的登記，回傳取消掉的時段（依時間排序）。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, slot_date, period FROM signups WHERE user_id=? AND slot_date>=? AND cancelled_at IS NULL",
                (user_id, today.isoformat()),
            ).fetchall()
            conn.executemany(
                "UPDATE signups SET cancelled_at=? WHERE id=?",
                [(now.isoformat(timespec="seconds"), row["id"]) for row in rows],
            )
        return sorted((Slot(date.fromisoformat(row["slot_date"]), row["period"]) for row in rows), key=lambda s: s.sort_key)

    def entries(self, date_from: date, date_to: date) -> list[Entry]:
        """[date_from, date_to] 之間（含兩端）所有有效的登記，依登記先後排序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.*, COALESCE(b.name, CASE WHEN s.user_id LIKE 'name:%' THEN substr(s.user_id, 6) END, '') "
                "AS name, b.player_id FROM signups s LEFT JOIN bindings b USING (user_id) "
                "WHERE s.slot_date BETWEEN ? AND ? AND s.cancelled_at IS NULL ORDER BY s.id",
                (date_from.isoformat(), date_to.isoformat()),
            ).fetchall()
        return [
            Entry(
                id=row["id"],
                chat_id=row["chat_id"],
                user_id=row["user_id"],
                name=row["name"] or "（未綁定）",
                slot=Slot(date.fromisoformat(row["slot_date"]), row["period"]),
                created_at=row["created_at"],
                note=row["note"],
                player_id=row["player_id"],
            )
            for row in rows
        ]

    def set_slot(
        self, slot: Slot, people: list[tuple[str, str]], now: datetime, canonical=lambda name: name
    ) -> tuple[list[str], list[str]]:
        """把一格直接改成 people（[(名字, 備註), ...]），管理用。

        不在名單裡的人取消，名單裡還沒登記的人加進去（有綁定用他的帳號，沒有用佔
        位 id），已經在的人只更新備註、保留原本的順序。不檢查人數上限。回傳
        (加進去的名字, 拿掉的名字)。canonical 把舊名字換成目前的名字，改名前後
        的寫法都對得上同一個人。
        """
        stamp = now.isoformat(timespec="seconds")
        people = [(canonical(name), note) for name, note in people]
        wanted = dict(people)
        with self._lock, self._connect() as conn:
            current = conn.execute(
                "SELECT s.id, COALESCE(b.name, CASE WHEN s.user_id LIKE 'name:%' THEN substr(s.user_id, 6) END, '') AS name "
                "FROM signups s LEFT JOIN bindings b USING (user_id) "
                "WHERE s.slot_date=? AND s.period=? AND s.cancelled_at IS NULL",
                (slot.day.isoformat(), slot.period),
            ).fetchall()
            present = {canonical(row["name"]): row["id"] for row in current}
            removed = [name for name in present if name not in wanted]
            conn.executemany(
                "UPDATE signups SET cancelled_at=? WHERE id=?", [(stamp, present[name]) for name in removed]
            )
            bound = {canonical(row["name"]): row["user_id"] for row in conn.execute("SELECT user_id, name FROM bindings")}
            added = []
            for name, note in people:
                if name in present:
                    conn.execute("UPDATE signups SET note=? WHERE id=?", (note, present[name]))
                    continue
                conn.execute(
                    "INSERT INTO signups (chat_id, user_id, slot_date, period, note, created_at) VALUES (?,?,?,?,?,?)",
                    (MANUAL_CHAT_ID, bound.get(name, NAME_PREFIX + name), slot.day.isoformat(), slot.period, note, stamp),
                )
                added.append(name)
        return added, removed

    def active_slots(self, date_from: date, date_to: date) -> set[Slot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT slot_date, period FROM signups WHERE slot_date BETWEEN ? AND ? AND cancelled_at IS NULL",
                (date_from.isoformat(), date_to.isoformat()),
            ).fetchall()
        return {Slot(date.fromisoformat(row["slot_date"]), row["period"]) for row in rows}

    def binding_for(self, user_id: str) -> tuple[str, int | None] | None:
        """(綁定的名字, 玩家 id)；沒綁定回 None。"""
        with self._connect() as conn:
            row = conn.execute("SELECT name, player_id FROM bindings WHERE user_id=?", (user_id,)).fetchone()
        return (row["name"], row["player_id"]) if row else None

    def bindings_without_player_id(self) -> list[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id, name FROM bindings WHERE player_id IS NULL").fetchall()
        return [(row["user_id"], row["name"]) for row in rows]

    def set_player_id(self, user_id: str, player_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE bindings SET player_id=? WHERE user_id=?", (player_id, user_id))

    def bind(
        self, user_id: str, name: str, line_name: str, now: datetime,
        player_id: int | None = None, aliases: tuple[str, ...] = (),
    ) -> bool:
        """把 LINE 帳號綁到一個 SOGA 玩家。

        正式成員以 player_id 為準（改名不影響），新人沒有 id 就以名字為準。這個
        玩家已經被別的帳號綁走的話回 False，不覆蓋——不然誰都能把別人搶過來，替
        他登記。aliases 是這個玩家用過的其他名字：班底表或舊登記若寫的是舊名字，
        那些佔位登記也一起歸給他。
        """
        with self._lock, self._connect() as conn:
            if player_id is not None:
                owner = conn.execute(
                    "SELECT user_id FROM bindings WHERE player_id=? OR name=?", (player_id, name)
                ).fetchone()
            else:
                owner = conn.execute("SELECT user_id FROM bindings WHERE name=?", (name,)).fetchone()
            if owner and owner["user_id"] != user_id:
                return False
            conn.execute(
                "INSERT INTO bindings (user_id, name, player_id, line_name, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name, player_id=excluded.player_id, "
                "line_name=excluded.line_name, updated_at=excluded.updated_at",
                (user_id, name, player_id, line_name, now.isoformat(timespec="seconds")),
            )
            # 固定班底的佔位登記換成他的帳號。他自己也登記了同一格的話，UPDATE OR
            # IGNORE 會跳過那一列（唯一索引），剩下的佔位就是重複的，直接取消。
            for alias in {name, *aliases}:
                placeholder = NAME_PREFIX + alias
                conn.execute(
                    "UPDATE OR IGNORE signups SET user_id=? WHERE user_id=? AND cancelled_at IS NULL",
                    (user_id, placeholder),
                )
                conn.execute(
                    "UPDATE signups SET cancelled_at=? WHERE user_id=? AND cancelled_at IS NULL",
                    (now.isoformat(timespec="seconds"), placeholder),
                )
            return True

    def seed_week(
        self, start: date, defaults: list[tuple[int, str, list[str]]], now: datetime, canonical=lambda name: name
    ) -> bool:
        """把固定班底帶入 start 那一週。已經帶過就不動，回傳是否這次有帶。

        defaults 是 [(星期幾 0=一, 時段, [名字...]), ...]。名字有綁定的就用他的
        LINE 帳號，沒有的用佔位 id。已經自己登記了同一格的人不會被重複加進去。
        班底不受人數上限限制——上限是給「剩下的位子」用的。
        """
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM seeded_weeks WHERE week_start=?", (start.isoformat(),)).fetchone():
                return False
            bound = {canonical(row["name"]): row["user_id"] for row in conn.execute("SELECT user_id, name FROM bindings")}
            stamp = now.isoformat(timespec="seconds")
            for weekday, period, names in defaults:
                day = (start + timedelta(days=weekday)).isoformat()
                for name in names:
                    conn.execute(
                        "INSERT OR IGNORE INTO signups (chat_id, user_id, slot_date, period, created_at) VALUES (?,?,?,?,?)",
                        (DEFAULT_CHAT_ID, bound.get(canonical(name), NAME_PREFIX + name), day, period, stamp),
                    )
            slots = ",".join(f"{weekday}{period}" for weekday, period, _ in defaults)
            conn.execute(
                "INSERT INTO seeded_weeks (week_start, slots, created_at) VALUES (?,?,?)", (start.isoformat(), slots, stamp)
            )
            return True

    def seeded_slots(self, start: date) -> list[tuple[int, str]] | None:
        """那週帶入時的固定時段 [(星期幾, 時段), ...]；還沒帶入過回 None。表上要
        照帶入當時的樣子列，班底檔後來改了也不影響已經開始的週。"""
        with self._connect() as conn:
            row = conn.execute("SELECT slots FROM seeded_weeks WHERE week_start=?", (start.isoformat(),)).fetchone()
        if row is None:
            return None
        return [(int(item[0]), item[1]) for item in row["slots"].split(",") if item]

    def save_chat(self, chat_id, chat_type, name, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chats (chat_id, chat_type, name, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (chat_id, chat_type, name, now.isoformat(timespec="seconds")),
            )

    def chat_names(self) -> dict[str, str]:
        with self._connect() as conn:
            return {row["chat_id"]: row["name"] for row in conn.execute("SELECT chat_id, name FROM chats")}

    def has_chat(self, chat_id) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone() is not None
