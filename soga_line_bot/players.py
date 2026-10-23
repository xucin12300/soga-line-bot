"""SOGA 的玩家名單，讀 NAS 上統計站那份 soga.db（唯讀）。

那份是 scheduler-soga 每天從計分電腦同步過去的（容器裡掛在 /soga-data），所以
今天第一次來打的新玩家，要等下一次同步才會出現在名單裡。

玩家會改名：history_player_info 的 id 不變，current_name 換掉，舊名字留在
history_name（JSON 陣列）。所以綁定記的是 id，顯示時再用 id 查目前的名字；
班底表或舊的登記裡寫的若是舊名字，也能透過 history_name 對回同一個人。

讀不到 soga.db 就當作空名單，bot 照常運作，只是名字不會跟著改名、也不標新人。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600


class SogaPlayers:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._current: dict[int, str] = {}  # id → 目前的名字
        self._by_name: dict[str, int] = {}  # 任何用過的名字 → id
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        with self._lock:
            if time.monotonic() - self._loaded_at > CACHE_SECONDS:
                self._current, self._by_name = self._load()
                self._loaded_at = time.monotonic()

    def names(self) -> set[str]:
        """目前所有正式成員的名字。"""
        self._refresh()
        return set(self._current.values())

    def player_id(self, name: str) -> int | None:
        """名字（目前的或用過的）對到的玩家 id；不是正式成員回 None。"""
        self._refresh()
        return self._by_name.get(name)

    def current_name(self, player_id: int | None) -> str | None:
        self._refresh()
        return self._current.get(player_id) if player_id is not None else None

    def aliases(self, player_id: int | None) -> tuple[str, ...]:
        """這個玩家用過的所有名字（含目前的）。"""
        self._refresh()
        return tuple(name for name, pid in self._by_name.items() if pid == player_id) if player_id is not None else ()

    def canonical(self, name: str) -> str:
        """把可能是舊名字的名字換成目前的名字；對不到就原樣回傳。"""
        return self.current_name(self.player_id(name)) or name

    def _load(self) -> tuple[dict[int, str], dict[str, int]]:
        if not self.db_path:
            return {}, {}
        try:
            # immutable=1：soga.db 是 WAL 模式，光 mode=ro 還是要在旁邊建 -shm 檔，
            # 唯讀掛載的目錄建不了，結果是 "unable to open database file"。這份是
            # 同步過來的副本、沒有人在原地寫它，當成不會變的檔案讀就好。
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True, timeout=5)
            try:
                rows = conn.execute("SELECT id, current_name, history_name FROM history_player_info").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("讀不到 SOGA 玩家名單 %s：%s", self.db_path, exc)
            return {}, {}

        current = {pid: name for pid, name, _ in rows}
        by_name: dict[str, int] = {}
        # 先放舊名字、最後放目前的名字：萬一某個舊名字後來被別人拿去當名字，
        # 以「現在叫這個名字的人」為準。
        for pid, _, history in rows:
            try:
                old_names = json.loads(history or "[]")
            except ValueError:
                old_names = []
            for old in old_names:
                by_name.setdefault(str(old), pid)
        for pid, name in current.items():
            by_name[name] = pid
        return current, by_name
