# -*- coding: utf-8 -*-
"""每轮 trace 的落盘、读取与轮转。

trace 是排查"她为什么这么答"的唯一结构化依据，也是这个项目最值钱的运维基建之一。
它自己坏了不能拖垮聊天（写失败一律静默），但也**不能无限长**（/api/stats 每天要读它）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import runtime  # noqa: E402


class TestTrace(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trace.jsonl")
        self._old_path = runtime._trace_path
        self._old_max = runtime.TRACE_MAX
        runtime._trace_path = lambda: self.path

    def tearDown(self):
        runtime._trace_path = self._old_path
        runtime.TRACE_MAX = self._old_max
        self.tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        runtime._trace_write({"kind": "chat", "n": 1})
        runtime._trace_write({"kind": "proactive", "n": 2, "sent": 0})
        items = runtime._trace_tail(10)
        self.assertEqual([i["n"] for i in items], [1, 2])
        self.assertEqual(items[1]["kind"], "proactive")

    def test_tail_returns_the_last_n_in_order(self):
        for i in range(20):
            runtime._trace_write({"n": i})
        self.assertEqual([i["n"] for i in runtime._trace_tail(5)],
                         [15, 16, 17, 18, 19])

    def test_tail_on_missing_file_is_empty(self):
        self.assertEqual(runtime._trace_tail(5), [])

    def test_broken_lines_are_skipped_not_fatal(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"n": 1}\n')
            f.write("这不是 JSON\n")
            f.write('{"n": 2}\n')
        self.assertEqual([i["n"] for i in runtime._trace_tail(10)], [1, 2])

    def test_write_failure_is_swallowed(self):
        """观测手段绝不能反过来把聊天搞挂：写不进去就悄悄算了。"""
        bad = os.path.join(self.tmp.name, "a-directory")
        os.makedirs(bad)
        runtime._trace_path = lambda: bad      # 指向目录 → 打开必然失败
        runtime._trace_write({"n": 1})         # 不该抛

    def test_file_stays_bounded_when_rotating(self):
        """轮转：以前 trace.jsonl 只增不减，会一直长下去。"""
        runtime.TRACE_MAX = 800
        for i in range(200):
            runtime._trace_write({"n": i, "pad": "x" * 40})
        self.assertLessEqual(os.path.getsize(self.path), runtime.TRACE_MAX + 200)
        # 砍掉的必须是旧的，最新的那条还在
        self.assertEqual(runtime._trace_tail(1)[0]["n"], 199)

    def test_rotation_does_not_produce_half_a_line(self):
        runtime.TRACE_MAX = 500
        for i in range(60):
            runtime._trace_write({"n": i})
        # 每一行都还必须能解析成 JSON
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.assertIn("n", json.loads(line))


if __name__ == "__main__":
    unittest.main()
