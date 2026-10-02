# -*- coding: utf-8 -*-
"""约定账本（agenda）的回归测试。

覆盖的正是把它写出来的那两个坑：
1. 相对时间必须按**那句话被说出的那天**解析成绝对日期 —— 否则第二天"明天"就滑到
   新的一天，承诺永远兑现不了；
2. 过期未办成的约定要落 missed、且绝不许顺延成新日子；办成的落 done 后不再注入。

全部是纯逻辑，不联网、不读真实 config。
"""
import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import agenda  # noqa: E402


class TestParseDate(unittest.TestCase):
    BASE = datetime.date(2026, 9, 29)          # 周二

    def test_iso_and_chinese_full_date(self):
        self.assertEqual(agenda.parse_date("2026-09-28 见面", self.BASE),
                         datetime.date(2026, 9, 25))
        self.assertEqual(agenda.parse_date("2026年10月1日回家", self.BASE),
                         datetime.date(2026, 10, 1))

    def test_month_day(self):
        self.assertEqual(agenda.parse_date("10月3号去玩", self.BASE),
                         datetime.date(2026, 10, 3))

    def test_relative_words(self):
        self.assertEqual(agenda.parse_date("明天来找我", self.BASE),
                         datetime.date(2026, 9, 30))
        self.assertEqual(agenda.parse_date("后天考试", self.BASE),
                         datetime.date(2026, 10, 1))
        self.assertEqual(agenda.parse_date("大后天出发", self.BASE),
                         datetime.date(2026, 10, 2))

    def test_relative_is_anchored_to_the_day_it_was_said(self):
        """同一句"明天"，说出来的那天不同 → 绝对日期必须跟着不同。

        这就是"她昨天说明天来、今天还说明天"那个 bug 的回归测试。
        """
        a = agenda.parse_date("明天来找你", datetime.date(2026, 9, 29))
        b = agenda.parse_date("明天来找你", datetime.date(2026, 9, 30))
        self.assertEqual(a, datetime.date(2026, 9, 30))
        self.assertEqual(b, datetime.date(2026, 10, 1))
        self.assertNotEqual(a, b)

    def test_next_weekday_and_weekend(self):
        self.assertEqual(agenda.parse_date("下周一交作业", self.BASE),
                         datetime.date(2026, 10, 5))
        self.assertEqual(agenda.parse_date("下周三", self.BASE),
                         datetime.date(2026, 10, 7))
        self.assertEqual(agenda.parse_date("周末打球", self.BASE),
                         datetime.date(2026, 10, 3))

    def test_national_day_and_month_end(self):
        self.assertEqual(agenda.parse_date("国庆出去玩", self.BASE),
                         datetime.date(2026, 10, 1))
        self.assertEqual(agenda.parse_date("月底结账", self.BASE),
                         datetime.date(2026, 9, 30))

    def test_bare_day_number(self):
        self.assertEqual(agenda.parse_date("28号放假", self.BASE),
                         datetime.date(2026, 9, 25))

    def test_unparseable_returns_none(self):
        """宁可漏、不可猜：解析不出来就不进账本（猜错比没有更糟）。"""
        for t in ["过两天再说", "下周有空的时候", "等我有空", "", "想你了"]:
            self.assertIsNone(agenda.parse_date(t, self.BASE), t)

    def test_bad_base_returns_none(self):
        self.assertIsNone(agenda.parse_date("明天", None))
        self.assertIsNone(agenda.parse_date("明天", "2026-09-29"))


class TestAgendaStateMachine(unittest.TestCase):
    """账本的状态机 + 注入块。用临时文件，不碰真实 data/agenda.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = agenda.AGENDA_PATH
        agenda.AGENDA_PATH = os.path.join(self.tmp.name, "agenda.json")
        agenda._CACHE["t"] = 0.0
        agenda._CACHE["text"] = ""

    def tearDown(self):
        agenda.AGENDA_PATH = self._old_path
        agenda._CACHE["t"] = 0.0
        agenda._CACHE["text"] = ""
        self.tmp.cleanup()

    def _write(self, items):
        with open(agenda.AGENDA_PATH, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False)

    def test_settle_marks_past_as_missed(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "去看她", "date": "2026-09-28"}])
        self.assertEqual(agenda.settle(today), 1)
        self.assertEqual(agenda._load()[0]["status"], agenda.STATUS_MISSED)
        # 幂等：再结算一次不该重复计数
        self.assertEqual(agenda.settle(today), 0)

    def test_past_promise_is_labelled_as_past_and_must_not_be_rescheduled(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "28号放假", "date": "2026-09-28"}])
        blk = agenda.block(today=today, use_cache=False)
        self.assertIn("已经过完的日子", blk)
        self.assertIn("别把日期改到新日子", blk)

    def test_future_promise_is_listed_with_today_inside_the_block(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "10月1日见面", "date": "2026-10-01"}])
        blk = agenda.block(today=today, use_cache=False)
        self.assertIn("还没到的", blk)
        self.assertIn("10月1日见面", blk)
        # 「今天是 X月X日」必须写在块里：只放 system 最前面隔了 4000 多字，注意力早散了
        self.assertIn("今天是 9月29日", blk)

    def test_done_promise_is_not_injected_any_more(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "10月1日见面", "date": "2026-10-01"}])
        self.assertIn("10月1日见面", agenda.block(today=today, use_cache=False))
        self.assertEqual(agenda.mark_done("见面"), 1)
        self.assertEqual(agenda.block(today=today, use_cache=False), "")

    def test_done_settle_does_not_touch_it(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "已办成的事", "date": "2026-09-28",
                      "status": agenda.STATUS_DONE}])
        self.assertEqual(agenda.settle(today), 0)
        self.assertEqual(agenda.block(today=today, use_cache=False), "")

    def test_out_of_window_promise_is_not_injected(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "很久以后的事", "date": "2027-06-01"}])
        self.assertEqual(agenda.block(today=today, use_cache=False), "")

    def test_malformed_date_is_dropped_not_crashed(self):
        today = datetime.date(2026, 9, 29)
        self._write([{"text": "没日期", "date": "不是日期"}])
        self.assertEqual(agenda.block(today=today, use_cache=False), "")

    def test_auto_done_requires_explicit_evidence(self):
        """保守到近乎悲观：只是提了一嘴、没有完成词，绝不许标成"办成了"。"""
        self._write([{"text": "10月1日见面", "date": "2026-10-01"}])
        mem = os.path.join(self.tmp.name, "memory.json")
        with open(mem, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "m1", "type": "event",
                                  "text": "10月1日见面", "time": "2026-09-29 10:00"}]},
                      f, ensure_ascii=False)
        self.assertEqual(agenda.auto_done_from_memory(mem), 0)

    def test_auto_done_with_explicit_completion_word(self):
        self._write([{"text": "10月1日见面", "date": "2026-10-01"}])
        mem = os.path.join(self.tmp.name, "memory.json")
        with open(mem, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "m1", "type": "event",
                                  "text": "10月1日见面已办成",
                                  "time": "2026-10-01 20:00"}]},
                      f, ensure_ascii=False)
        self.assertEqual(agenda.auto_done_from_memory(mem), 1)
        self.assertEqual(agenda._load()[0]["status"], agenda.STATUS_DONE)

    def test_non_event_memories_never_enter_the_ledger(self):
        """只对 type=event 的条目解析日期；facts 一律不动。"""
        mem = os.path.join(self.tmp.name, "memory.json")
        with open(mem, "w", encoding="utf-8") as f:
            json.dump({"items": [{"id": "m1", "type": "fact",
                                  "text": "他10月1日生日",
                                  "time": "2026-09-29 10:00"}]},
                      f, ensure_ascii=False)
        agenda.refresh(mem)
        self.assertEqual(agenda._load(), [])


if __name__ == "__main__":
    unittest.main()
