# -*- coding: utf-8 -*-
"""每轮 trace 的落盘、读取与轮转。

trace 是排查"她为什么这么答"的唯一结构化依据，也是这个项目最值钱的运维基建之一。
它自己坏了不能拖垮聊天（写失败一律静默），但也**不能无限长**（/api/stats 每天要读它）。
"""
import json
import os
import sys
import tempfile
import threading
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

    def test_trim_runs_under_the_same_lock_as_append(self):
        """确定性检查轮转有没有在锁里跑。

        轮转是"读全文 → 写回前半"，必须和 append 串在**同一把锁**里；否则另一条
        线程刚追加的记录会随后半段一起被丢掉，读的一刻还可能读到半行 JSON。
        这里直接替掉 _trace_trim，看它被调用时锁是不是已经握在手里 ——
        比"多线程跑一遍盼着它炸"可靠得多。
        """
        orig = runtime._trace_trim
        seen = {}

        def spy(path):
            seen["locked"] = runtime._TRACE_LOCK.locked()
            return orig(path)

        runtime._trace_trim = spy
        try:
            runtime._trace_write({"n": 1})
        finally:
            runtime._trace_trim = orig
        self.assertTrue(seen.get("locked"), "轮转没在锁里跑（会和 append 交错）")

    def test_concurrent_writes_never_produce_torn_lines(self):
        """并发写 + 频繁轮转之下，文件里不许出现半行 JSON、也不许重复。

        注：**不断言全局递增** —— 8 条线程各写自己的序列，交错顺序本来就不确定，
        断言排序是在测"调度运气"而不是测代码。
        """
        runtime.TRACE_MAX = 4000          # 压小，让轮转在测试里频繁发生
        n_threads, per_thread = 8, 60

        def worker(k):
            for i in range(per_thread):
                runtime._trace_write({"n": k * 1000 + i, "pad": "x" * 30})

        threads = [threading.Thread(target=worker, args=(k,))
                   for k in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with open(self.path, encoding="utf-8") as f:
            raw = [ln for ln in (l.strip() for l in f) if ln]
        self.assertTrue(raw, "轮转把文件砍空了")
        recs = [json.loads(ln) for ln in raw]     # 半行 JSON 会在这里抛
        ns = [r["n"] for r in recs]
        self.assertEqual(len(ns), len(set(ns)), "同一条记录被写了两遍")
        # 轮转只砍前面，留下的必须是最近的一批：最后一条属于最后收尾的线程
        last_writes = {k * 1000 + per_thread - 1 for k in range(n_threads)}
        self.assertIn(ns[-1], last_writes,
                      "文件尾部不是最近写入的那批（轮转把新的砍了）")
        self.assertLessEqual(os.path.getsize(self.path),
                             runtime.TRACE_MAX + 400)


if __name__ == "__main__":
    unittest.main()
