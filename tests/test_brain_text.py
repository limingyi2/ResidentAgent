# -*- coding: utf-8 -*-
"""对话出口的文本处理：分段、清洗、静默判定。

这些都是"必经出口"上的逻辑，做错了直接面向用户：模型漏出的 <think>、
时间戳前缀、引号、名字前缀，以及"无"的各种变体。不联网。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import brain  # noqa: E402


class TestSplitText(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(brain.split_text("你好"), ["你好"])

    def test_empty_input(self):
        self.assertEqual(brain.split_text(""), [])
        self.assertEqual(brain.split_text(None), [])

    def test_no_piece_exceeds_the_limit(self):
        """长度必须有保证：一大段没有句号的话也不能切出超长段。"""
        for text in ("句子。" * 200, "x" * 1000, "一，二，三，" * 100):
            for piece in brain.split_text(text, limit=50):
                self.assertLessEqual(len(piece), 50, text[:20])

    def test_prefers_sentence_end_over_comma(self):
        text = "第一句话。第二句话。" + "x" * 100
        pieces = brain.split_text(text, limit=20)
        self.assertTrue(pieces[0].endswith("。"))
        self.assertEqual(pieces[0], "第一句话。第二句话。")

    def test_hard_break_when_there_is_no_punctuation(self):
        pieces = brain.split_text("x" * 95, limit=30)
        self.assertTrue(all(len(p) <= 30 for p in pieces))
        self.assertEqual("".join(pieces), "x" * 95)

    def test_content_is_preserved(self):
        text = "他说：这件事就这样吧。然后我们去了别的地方，聊了很久很久。"
        self.assertEqual("".join(brain.split_text(text, limit=12)), text)


class TestSplitMessages(unittest.TestCase):
    def test_blank_lines_dropped(self):
        self.assertEqual(brain.split_messages("一\n\n二\n"), ["一", "二"])

    def test_empty_input(self):
        self.assertEqual(brain.split_messages(""), [])
        self.assertEqual(brain.split_messages(None), [])

    def test_voice_tag_merges_with_its_transcript(self):
        """[voice:…] 和紧跟的一句转写合成一条 —— 拆开的话 App 会在语音条后
        再跟一条复读的文字泡。"""
        out = brain.split_messages("[voice:zv_piper]\n你好呀")
        self.assertEqual(len(out), 1)
        self.assertIn("[voice:", out[0])
        self.assertIn("你好呀", out[0])

    def test_trailing_voice_tag_stays_alone(self):
        out = brain.split_messages("先说话\n[voice:zv_piper]")
        self.assertEqual(len(out), 2)

    def test_full_width_colon_in_voice_tag(self):
        out = brain.split_messages("[voice：zv_piper]\n早")
        self.assertEqual(len(out), 1)


class TestClean(unittest.TestCase):
    def test_strips_closed_think_block(self):
        self.assertEqual(brain._clean("<think>想了一会儿</think>你好"), "你好")

    def test_strips_unclosed_think_block(self):
        """模型有时只吐 <think> 不闭合（被 max_tokens 截断），
        残留会直接发给用户 —— 这是所有输出的必经出口，必须处理。"""
        self.assertEqual(brain._clean("你好<think>还在想"), "你好")
        self.assertEqual(brain._clean("</think>你好"), "你好")

    def test_strips_leading_time_tag(self):
        self.assertEqual(brain._clean("[09-29 06:18] 早啊"), "早啊")
        self.assertEqual(brain._clean("[2026-09-29 06:18] 早啊"), "早啊")

    def test_strips_name_prefix_and_wrapping_quotes(self):
        # 人设名从 persona_store 动态取，测试里显式打桩，不依赖仓库里预置的角色名
        self.assertEqual(brain._clean("“早啊”"), "早啊")
        self.assertEqual(brain._clean("「早啊」"), "早啊")

    def test_strips_configured_name_prefix(self):
        """角色名改了这里要跟着改 —— 名字从人设取，不写死。"""
        import persona_store
        orig = persona_store.load
        persona_store.load = lambda *a, **k: {
            "name": "阿柚", "nicknames": ["小柚"]}
        brain._NAME_PREFIX_CACHE["sig"] = None
        try:
            self.assertEqual(brain._clean("阿柚：早啊"), "早啊")
            self.assertEqual(brain._clean("小柚：早啊"), "早啊")
            self.assertEqual(brain._clean("一\n小柚：二", keep_newlines=True),
                             "一\n二")
            # 不是人设里的名字就不动
            self.assertEqual(brain._clean("别人：早啊"), "别人：早啊")
        finally:
            persona_store.load = orig
            brain._NAME_PREFIX_CACHE["sig"] = None
            brain._NAME_PREFIX_CACHE["re"] = None

    def test_keeps_newlines_preserves_segments(self):
        """她分条的短消息靠换行拆条连发，这条路径不能把换行压掉。"""
        self.assertEqual(brain._clean("第一句\n第二句", keep_newlines=True),
                         "第一句\n第二句")

    def test_keep_newlines_still_cleans_every_line(self):
        out = brain._clean("[09-29 06:18] 一\n“二”", keep_newlines=True)
        self.assertEqual(out, "一\n二")

    def test_without_keep_newlines_newlines_collapse(self):
        self.assertEqual(brain._clean("一\n二", keep_newlines=False), "一 二")

    def test_handles_none(self):
        self.assertEqual(brain._clean(None), "")


class TestIsSilence(unittest.TestCase):
    def test_all_the_shapes_of_nothing_to_say(self):
        for t in ["", "   ", "无", "无无", "无无无", "（无）", "无。", "[无]"]:
            self.assertTrue(brain._is_silence(t), repr(t))

    def test_real_speech_is_not_silence(self):
        for t in ["无聊", "无所谓的", "在呢", "无糖可乐好喝"]:
            self.assertFalse(brain._is_silence(t), repr(t))


class TestDaySegment(unittest.TestCase):
    def test_segments(self):
        self.assertEqual(brain._day_segment(6), "早上")
        self.assertEqual(brain._day_segment(12), "中午")
        self.assertEqual(brain._day_segment(15), "下午")
        self.assertEqual(brain._day_segment(20), "晚上")
        self.assertEqual(brain._day_segment(3), "深夜")


if __name__ == "__main__":
    unittest.main()
