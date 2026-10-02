# -*- coding: utf-8 -*-
"""历史摘要（recap）的水位线与分批逻辑。

pending_rows 是纯函数：决定"哪些行该压、压到哪一行"。它管着两件事 ——
不重复压（水位线）、不把某一天截成两半（同一天被压成两段摘要，话题会在中间断掉）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import recap_store  # noqa: E402


def _rows(spec):
    """spec: [(日期, 条数)] → 连续的 (行号, 时间, 角色, 正文) 列表。"""
    out, n = [], 0
    for day, cnt in spec:
        for i in range(cnt):
            n += 1
            out.append((n, "%s 10:%02d" % (day, i % 60), "user", "第%d条" % n))
    return out


class TestPendingRows(unittest.TestCase):
    def test_nothing_to_compress_inside_the_recent_window(self):
        rows = _rows([("2026-09-29", 30)])
        sel, upto = recap_store.pending_rows(rows, 0)
        # end = 30 - KEEP_RECENT(24) = 6
        self.assertEqual(len(sel), 6)
        self.assertEqual(upto, 6)

    def test_watermark_prevents_recompressing(self):
        rows = _rows([("2026-09-29", 30)])
        sel, upto = recap_store.pending_rows(rows, 6)
        self.assertEqual(sel, [])
        self.assertEqual(upto, 6)

    def test_watermark_is_monotonic_when_new_lines_arrive(self):
        rows = _rows([("2026-09-29", 40)])
        sel, upto = recap_store.pending_rows(rows, 6)
        self.assertEqual(upto, 16)
        rows2 = _rows([("2026-09-29", 45)])
        sel2, upto2 = recap_store.pending_rows(rows2, upto)
        self.assertEqual((sel2[0][0], upto2), (17, 21))

    def test_watermark_resets_when_the_log_was_cleared(self):
        """存档被清空/换过之后，总行数小于水位线 → 必须复位重来，不能漏也不能重复。"""
        rows = _rows([("2026-09-29", 10)])
        sel, upto = recap_store.pending_rows(rows, 999)
        self.assertEqual(sel, [])
        self.assertEqual(upto, 0)

    def test_empty_log(self):
        self.assertEqual(recap_store.pending_rows([], 5), ([], 0))

    def test_batch_cut_lands_on_a_day_boundary(self):
        """一批压不完时，切点必须落在某一天的结尾 —— 不能把一天截成两段摘要。

        注意：为了凑齐整天，这一批可以**超过** BATCH_MAX（这里 300 > 240），
        这是刻意的 —— 天对齐优先于字数上限。
        """
        per_day = 150
        rows = _rows([("2026-09-01", per_day),
                      ("2026-09-02", per_day),
                      ("2026-09-03", per_day),
                      ("2026-09-04", 24)])          # 末尾 24 条是保留窗口
        sel, upto = recap_store.pending_rows(rows, 0)
        # 选中的最后一行应该是 9-02 那天的最后一条，而不是 9-03 的某一条
        self.assertEqual(sel[-1][1][:10], "2026-09-02")
        self.assertEqual(len(sel), 2 * per_day)
        self.assertEqual(upto, sel[-1][0])


class TestDayAlignment(unittest.TestCase):
    def test_a_single_oversized_day_is_cut_hard(self):
        """某一天自己就超过上限 → 必须硬切。

        不切的话这一整天会一次性喂给模型，而 compress_day 会把正文截到 8000 字，
        后面的内容等于从没被压过 —— 水位线却已经推过去了，那些行被永久跳过。
        （老代码写的是 `if cut == 0`，而 cut 第一轮就至少是 1，那个分支从未生效。）
        """
        rows = _rows([("2026-09-01", 600)])
        sel, upto = recap_store.pending_rows(rows, 0)
        self.assertEqual(len(sel), recap_store.BATCH_MAX)
        self.assertEqual(upto, recap_store.BATCH_MAX)

    def test_an_oversized_day_is_fully_covered_across_ticks(self):
        """硬切之后要能一轮轮接着压，最终一条都不漏（水位线连续推进）。"""
        rows = _rows([("2026-09-01", 600)])
        upto, seen, ticks = 0, 0, 0
        while True:
            sel, upto = recap_store.pending_rows(rows, upto)
            ticks += 1
            if not sel:
                break
            seen += len(sel)
            self.assertLessEqual(len(sel), recap_store.BATCH_MAX)
            self.assertLess(ticks, 20, "压不完：水位线没有推进")
        self.assertEqual(seen, 576)          # 600 - KEEP_RECENT(24)
        self.assertEqual(upto, 576)


if __name__ == "__main__":
    unittest.main()
