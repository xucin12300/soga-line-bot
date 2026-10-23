"""把群組裡的一句話解析成指令。

格式照群組原本手寫的那張表：一行是「星期 + 時段」，例如「五晚」。

    +五晚            登記週五晚上
    +一晚 三晚       一次登記好幾個
    +六晚 晚點到     時段後面的字是自己的備註（表上變成「名字（晚點到）」）
    -五晚            取消
    取消             取消自己今天起所有的登記（「全部取消」也可以）
    名單             看這週的表
    綁定 小魚        把自己的 LINE 帳號對到 SOGA 的名字
    說明
    覆蓋 + 整張表    管理員把整張表改成貼上的樣子（見 app.py 的 overwrite）
    班底             管理員看目前的固定班底表（「DEFAULT」也可以）
    修改班底 + 表    管理員換掉固定班底表（「修改DEFAULT」也可以）

星期是 一～日（「天」也算日），時段是 早 午 晚 夜。日期是今天起算最近的那
一天：今天週五打「+五晚」是今晚，打「+四晚」就是下週四。

只有符合上面格式的訊息才算指令，其餘聊天一律不理——bot 放在群組裡，不能每句
話都回。全形的＋－會先轉成半形。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

WEEKDAYS = "一二三四五六日"
NOTE_MAX_LENGTH = 15
# 同一天裡的排序，也是表上的順序。
PERIODS = "早午晚夜"

_SLOT_COMMAND = re.compile(r"^([+\-])\s*((?:[一二三四五六日天]\s*[早午晚夜][\s,，、]*)+)")
_SLOT = re.compile(r"([一二三四五六日天])\s*([早午晚夜])")
_BIND = re.compile(r"^綁定\s*(\S{0,20})$")
_BOARD_WORDS = {"名單", "表", "登記表"}
_CANCEL_ALL_WORDS = {"取消", "全部取消"}
_HELP_WORDS = {"說明", "指令", "help"}
_LEADING_MENTION = re.compile(r"^@\S+\s*")


@dataclass(frozen=True, order=True)
class Slot:
    day: date
    period: str

    @property
    def key(self) -> str:
        """表上的寫法，例如「五晚」。"""
        return WEEKDAYS[self.day.weekday()] + self.period

    @property
    def sort_key(self) -> tuple:
        return (self.day, PERIODS.index(self.period))


@dataclass(frozen=True)
class Command:
    action: str  # add / remove / cancel_all / board / bind / help / overwrite / show_defaults / set_defaults
    slots: tuple[Slot, ...] = ()
    name: str = ""
    body: str = ""  # overwrite 貼上的整張表；add 時是備註


def parse(text: str, today: date) -> Command | None:
    """解析一句話。不是指令就回 None（bot 不回話）。"""
    text = _LEADING_MENTION.sub("", unicodedata.normalize("NFKC", text).strip())

    if text.startswith("覆蓋"):
        return Command("overwrite", body=text[2:])
    for prefix in ("修改班底", "修改DEFAULT"):
        if text.upper().startswith(prefix):
            return Command("set_defaults", body=text[len(prefix):])
    if text.upper() in ("班底", "DEFAULT"):
        return Command("show_defaults")

    match = _SLOT_COMMAND.match(text)
    if match:
        slots: list[Slot] = []
        for day_char, period in _SLOT.findall(match.group(2)):
            slot = Slot(next_weekday(today, WEEKDAYS.index("日" if day_char == "天" else day_char)), period)
            if slot not in slots:
                slots.append(slot)
        if match.group(1) == "-":
            return Command("remove", tuple(slots))
        # 時段後面的字當備註。空白換成無空格、去掉使用者自己加的括號，免得表上
        # 變成「名字（（晚點到））」；太長截斷，表才不會被一句話撐爆。
        note = re.sub(r"\s+", "", text[match.end():]).strip("()（）")[:NOTE_MAX_LENGTH]
        return Command("add", tuple(slots), body=note)

    if text in _BOARD_WORDS:
        return Command("board")
    if text in _CANCEL_ALL_WORDS:
        return Command("cancel_all")
    match = _BIND.match(text)
    if match:
        return Command("bind", name=match.group(1))
    if text.lower() in _HELP_WORDS:
        return Command("help")
    return None


def next_weekday(today: date, weekday: int) -> date:
    """今天起算最近的星期幾（今天就是那天的話就是今天）。"""
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


_BOARD_LINE = re.compile(r"^([一二三四五六日])([早午晚夜])(.*)$")
_BOARD_HEADER = re.compile(r"(\d{2})/(\d{1,2})/(\d{1,2})\s*-")
BOM = chr(0xFEFF)
_NOTE = re.compile(r"^(.+?)[（(](.*)[)）]$")


def parse_board(text: str) -> list[tuple[int, str, list[tuple[str, str]]]]:
    """把一整張表解析成 [(星期幾 0=一, 時段, [(名字, 備註), ...]), ...]。

    格式跟群組裡那張一樣，一行一格「五晚 榮恩 兔子」，名字後面可以接括號備註
    「兔子（1415到）」。看不懂的行（例如開頭的日期那行）略過。
    """
    result = []
    for raw in text.splitlines():
        match = _BOARD_LINE.match(raw.strip().lstrip(BOM))
        if not match:
            continue
        people = []
        for token in match.group(3).split():
            note = _NOTE.match(token)
            people.append((note.group(1), note.group(2)) if note else (token, ""))
        result.append((WEEKDAYS.index(match.group(1)), match.group(2), people))
    return result


def board_week(text: str) -> date | None:
    """表開頭「26/10/19 - 26/10/25」那行指的週（週一）；沒有那行回 None。"""
    match = _BOARD_HEADER.search(text)
    if not match:
        return None
    try:
        return week_start(date(2000 + int(match.group(1)), int(match.group(2)), int(match.group(3))))
    except ValueError:
        return None
