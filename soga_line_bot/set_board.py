"""直接把某一週的表改成指定的樣子（管理用）。

群組外面已經排好的表要搬進來時用。在 NAS 上：

    docker exec -i soga-line-bot python -m soga_line_bot.set_board < 表.txt
    docker exec -i soga-line-bot python -m soga_line_bot.set_board --week 2026-10-26 < 表.txt
    docker exec -i soga-line-bot python -m soga_line_bot.set_board --dry-run < 表.txt

表的格式跟群組裡那張一樣，一行一格：「五晚 榮恩 兔子 大樹 安妮」。名字後面可以
接括號備註，例如「兔子（1415到）」。開頭那行「26/10/19 - 26/10/25」有沒有都可以，
週由 --week 決定（預設這週）。

只改有列出來的時段：沒列的時段不動；列出來但沒有名字的時段會被清空。那週還沒
帶入班底的話先帶入，不然之後才帶入的班底會疊在改好的表上。不檢查人數上限。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta

from .app import DEFAULT_WEEKLY_DEFAULTS_PATH, resolve_weekly_defaults
from .parser import TZ, Slot, parse_board, week_start
from .store import SignupStore

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", help="那週任何一天 YYYY-MM-DD，預設這週")
    parser.add_argument("--dry-run", action="store_true", help="只印出會怎麼改，不寫入")
    args = parser.parse_args()

    now = datetime.now(TZ)
    start = week_start(date.fromisoformat(args.week) if args.week else now.date())
    board = parse_board(sys.stdin.buffer.read().decode("utf-8-sig"))
    if not board:
        raise SystemExit("沒讀到任何一行「星期＋時段 名字…」")

    for weekday, period, people in board:
        slot = Slot(start + timedelta(days=weekday), period)
        shown = " ".join(f"{n}（{note}）" if note else n for n, note in people)
        print(f"{slot.key}（{slot.day:%m/%d}）→ {shown}")
    if args.dry_run:
        print("（dry run，沒有寫入）")
        return

    store = SignupStore(os.getenv("SOGA_LINE_BOT_DB_PATH", "/data/line_bot.db"))
    defaults = resolve_weekly_defaults(os.getenv("SOGA_WEEKLY_DEFAULTS_PATH", DEFAULT_WEEKLY_DEFAULTS_PATH))
    if defaults and store.seed_week(start, defaults, now):
        print(f"先帶入 {start} 那週的班底")
    for weekday, period, people in board:
        slot = Slot(start + timedelta(days=weekday), period)
        added, removed = store.set_slot(slot, people, now)
        if added or removed:
            print(f"{slot.key}: +{' '.join(added) or '-'}  -{' '.join(removed) or '-'}")
    print("完成")


if __name__ == "__main__":
    main()
