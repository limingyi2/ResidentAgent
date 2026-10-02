# -*- coding: utf-8 -*-
"""主动搭话的规则层：静默时段、频率自适应、重复内容闸门、客户端 IP。

这几个是"自治行为失控"那一类事故的唯一防线（凌晨连发 8 条、把刚说过的话再说一遍），
所以每一条都值得钉住。全部纯逻辑，不联网。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import proactive  # noqa: E402


class TestQuietHours(unittest.TestCase):
    def test_parses_hhmm(self):
        self.assertEqual(proactive._parse_hhmm("23:00"), 23 * 60)
        self.assertEqual(proactive._parse_hhmm("7:30"), 7 * 60 + 30)
        self.assertEqual(proactive._parse_hhmm("00:00"), 0)
        self.assertEqual(proactive._parse_hhmm("24:00"), 1440)

    def test_rejects_bad_values(self):
        for s in ["", None, "25:00", "12:60", "24:01", "abc", "1200", "23-00"]:
            self.assertIsNone(proactive._parse_hhmm(s), repr(s))

    def test_quiet_window_crossing_midnight(self):
        """23:00~07:00 必须跨零点生效 —— 凌晨连发就是这么来的。"""
        for cur in (23 * 60, 23 * 60 + 59, 0, 30, 6 * 60 + 59):
            self.assertTrue(proactive._in_quiet("23:00", "07:00", cur=cur), cur)
        for cur in (7 * 60, 12 * 60, 22 * 60 + 59):
            self.assertFalse(proactive._in_quiet("23:00", "07:00", cur=cur), cur)

    def test_quiet_window_within_one_day(self):
        self.assertTrue(proactive._in_quiet("13:00", "14:00", cur=13 * 60 + 30))
        self.assertFalse(proactive._in_quiet("13:00", "14:00", cur=15 * 60))

    def test_broken_config_does_not_mute_her_all_day(self):
        """解析不出来、或起止相等，一律当"没设静默"。

        宁可照常说话让用户去改配置，也不能猜错把她整天闷住（这是刻意的取舍）。
        """
        for a, b in [("bad", "07:00"), ("23:00", "bad"), ("23:00", "23:00"),
                     ("", ""), (None, None)]:
            self.assertFalse(proactive._in_quiet(a, b, cur=12 * 60), (a, b))


class TestRepeatGate(unittest.TestCase):
    """重复内容闸门：写入前比对她最近说过的 12 句。"""

    def setUp(self):
        self._orig = proactive._recent_her_texts

    def tearDown(self):
        proactive._recent_her_texts = self._orig

    def _recent(self, texts):
        proactive._recent_her_texts = lambda n=12: list(texts)

    def test_exact_repeat_is_blocked(self):
        self._recent(["你在干嘛呀"])
        self.assertTrue(proactive._is_repeat_of_recent("你在干嘛呀"))

    def test_truncated_repeat_is_blocked(self):
        """她有时把刚说的一整段截个第一行再发一次 —— 那也要拦。"""
        self._recent(["我刚从图书馆回来\n先去洗个澡"])
        self.assertTrue(proactive._is_repeat_of_recent("我刚从图书馆回来"))

    def test_new_sentence_passes(self):
        self._recent(["你在干嘛呀"])
        self.assertFalse(proactive._is_repeat_of_recent("我今天好累"))

    def test_short_fragments_do_not_trigger_the_substring_rule(self):
        """少于 8 个字的片段不参与子串判定，否则"在吗"会到处误伤。"""
        self._recent(["我刚刚在图书馆看书"])
        self.assertFalse(proactive._is_repeat_of_recent("在吗"))

    def test_empty_is_never_a_repeat(self):
        self._recent(["随便什么"])
        self.assertFalse(proactive._is_repeat_of_recent(""))
        self.assertFalse(proactive._is_repeat_of_recent(None))

    def test_timestamp_prefix_does_not_defeat_dedup(self):
        """回归：_norm_text 里的正则以前要求**两个**连字符，而她自己写的标签是
        `[09-29 06:18]`（只有一个），于是"去时间戳前缀"从来没生效过。"""
        self.assertEqual(proactive._norm_text("[09-29 06:18] 早啊"), "早啊")
        self.assertEqual(proactive._norm_text("[2026-09-29 06:18] 早啊"), "早啊")
        self.assertEqual(proactive._norm_text(" 早  啊 "), "早啊")
        self._recent(["[09-29 06:18] 你在干嘛呀"])
        self.assertTrue(proactive._is_repeat_of_recent("你在干嘛呀"))


class TestAdaptiveInterval(unittest.TestCase):
    """频率自适应：按最近 10 次主动消息的"被回应率"伸缩间隔。"""

    def setUp(self):
        self._orig = proactive._proactive_stats_load

    def tearDown(self):
        proactive._proactive_stats_load = self._orig

    def _history(self, replied, total):
        sent = ([{"ts": 0, "replied": 1}] * replied
                + [{"ts": 0, "replied": 0}] * (total - replied))
        proactive._proactive_stats_load = lambda: {"sent": sent,
                                                  "last_user_ts": 0}

    def test_no_history_is_neutral(self):
        proactive._proactive_stats_load = lambda: {"sent": [], "last_user_ts": 0}
        self.assertEqual(proactive._proactive_interval_multiplier(), 1.0)

    def test_high_response_rate_keeps_the_interval(self):
        self._history(6, 10)
        self.assertEqual(proactive._proactive_interval_multiplier(), 1.0)

    def test_medium_response_rate_stretches(self):
        self._history(3, 10)
        self.assertEqual(proactive._proactive_interval_multiplier(), 1.5)

    def test_low_response_rate_stretches_most(self):
        self._history(1, 10)
        self.assertEqual(proactive._proactive_interval_multiplier(), 2.0)


class TestClientIp(unittest.TestCase):
    """他从哪连进来的 —— 天气城市跟着这个 IP 走。"""

    def setUp(self):
        self._old = dict(proactive._LAST_IP)

    def tearDown(self):
        proactive._LAST_IP.clear()
        proactive._LAST_IP.update(self._old)

    def test_public_ip_is_remembered(self):
        proactive.note_client_ip("47.114.58.45")
        self.assertEqual(proactive._his_ip(), "47.114.58.45")

    def test_internal_addresses_are_ignored(self):
        """桌宠走 SSH 隧道进来是 127.0.0.1 —— 那不是"他在哪"。"""
        for ip in ["127.0.0.1", "127.1.2.3", "10.0.0.5", "192.168.1.7",
                   "172.16.0.1", "172.31.255.1", "169.254.1.1", "::1", "", None]:
            proactive._LAST_IP["ip"] = ""
            proactive.note_client_ip(ip)
            self.assertEqual(proactive._his_ip(), "", repr(ip))


if __name__ == "__main__":
    unittest.main()
