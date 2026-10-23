import unittest
from datetime import date

from soga_line_bot.parser import Slot, parse

# 2026-10-23 是星期五。
TODAY = date(2026, 10, 23)


class ParseTest(unittest.TestCase):
    def slots(self, text):
        command = parse(text, TODAY)
        self.assertIsNotNone(command, text)
        return command.action, [s.key + s.day.strftime("@%m%d") for s in command.slots]

    def test_add_single(self):
        self.assertEqual(self.slots("+五晚"), ("add", ["五晚@1023"]))
        self.assertEqual(self.slots("＋六午"), ("add", ["六午@1024"]))
        self.assertEqual(self.slots("+ 日 夜"), ("add", ["日夜@1025"]))
        self.assertEqual(self.slots("+天早"), ("add", ["日早@1025"]))

    def test_past_weekday_is_next_week(self):
        self.assertEqual(self.slots("+一晚"), ("add", ["一晚@1026"]))
        self.assertEqual(self.slots("+四晚"), ("add", ["四晚@1029"]))

    def test_multiple_slots(self):
        self.assertEqual(self.slots("+一晚 三晚、六午"), ("add", ["一晚@1026", "三晚@1028", "六午@1024"]))
        self.assertEqual(self.slots("+五晚五晚"), ("add", ["五晚@1023"]))

    def test_remove(self):
        self.assertEqual(self.slots("-五晚"), ("remove", ["五晚@1023"]))
        self.assertEqual(self.slots("－五晚"), ("remove", ["五晚@1023"]))

    def test_trailing_text_ignored(self):
        self.assertEqual(self.slots("+五晚 會晚點到"), ("add", ["五晚@1023"]))

    def test_other_commands(self):
        self.assertEqual(parse("名單", TODAY).action, "board")
        self.assertEqual(parse("表", TODAY).action, "board")
        self.assertEqual(parse("說明", TODAY).action, "help")
        self.assertEqual(parse("取消", TODAY).action, "cancel_all")
        self.assertEqual(parse("全部取消", TODAY).action, "cancel_all")
        self.assertIsNone(parse("取消了啦", TODAY))
        bind = parse("綁定 小魚", TODAY)
        self.assertEqual((bind.action, bind.name), ("bind", "小魚"))
        self.assertEqual(parse("綁定", TODAY).name, "")
        self.assertEqual(parse("@SogaBot +五晚", TODAY).action, "add")

    def test_ignores_chat(self):
        for text in ["大家好", "五晚有人嗎", "+1", "+五", "名單呢", "綁定 小魚 謝謝", "+五晚上要打嗎"]:
            command = parse(text, TODAY)
            # 「+五晚上要打嗎」開頭就是 +五晚，照登記算；其他都不是指令。
            if text.startswith("+五晚"):
                self.assertEqual(command.action, "add")
            else:
                self.assertIsNone(command, text)

    def test_slot_sort(self):
        a, b = Slot(date(2026, 10, 24), "晚"), Slot(date(2026, 10, 24), "午")
        self.assertLess(b.sort_key, a.sort_key)


if __name__ == "__main__":
    unittest.main()
