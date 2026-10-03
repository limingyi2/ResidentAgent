# -*- coding: utf-8 -*-
"""语音：情绪指令怎么从她的回复里到 TTS，以及为什么缓存 key 必须带它。

"听着假"的根因是 TTS 拿到没有情绪指令的文本会照着念，像播报。修法是她自己
标（语气：xxx），合成时抽走当 instruction。

三处都容易悄悄坏掉，各钉一条：
- 标记没被剥掉→ 语音条下方会显示"（语气：懒洋洋）"这种漏给用户的东西
- 缓存 key 漏了 instruction → 第二条情绪命中第一条的文件，听着还是没变
- 切音色时 instruction 没跟着换 → 上一条音色的情绪串到下一条上
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "brain"))

import reply                                          # noqa: E402
import voice                                          # noqa: E402


class TestExtractMood(unittest.TestCase):
    def test_reads_and_strips(self):
        txt, mood = reply.extract_mood(
            "刚下课（语气：懒洋洋）")
        self.assertEqual(mood, "懒洋洋")
        self.assertEqual(txt, "刚下课")

    def test_all_three_brackets(self):
        """模型不一定会打半角括号，方括号和方头括号也得收。"""
        for t in ("话（语气：犯困）", "话(语气:犯困)",
                  "话[语气：犯困]", "话【语气：犯困】"):
            _, mood = reply.extract_mood(t)
            self.assertEqual(mood, "犯困", t)

    def test_plain_text_untouched(self):
        """没有标记时原样返回 —— 不能凭空加东西。"""
        t = "刚下课，外面下了点雨"
        self.assertEqual(reply.extract_mood(t), (t, ""))

    def test_parentheses_that_are_not_mood_survive(self):
        """日常括号不能被吃掉。（明天考试）不是语气标记。"""
        t = "刚下课（明天考试）"
        txt, mood = reply.extract_mood(t)
        self.assertEqual(mood, "")
        self.assertEqual(txt, t)

    def test_long_mood_capped(self):
        """过长的 instruction 收窄：它是提示注入面，也是给 TTS 的噪声。"""
        _, mood = reply.extract_mood("话（语气：" + "啊" * 200 + "）")
        self.assertLessEqual(len(mood), 40)

    def test_only_first_marker(self):
        """一句语音一个语气，取第一个，剩下的留着（总比全剥干净好）。"""
        txt, mood = reply.extract_mood("话（语气：懒洋洋）又（语气：开心）")
        self.assertEqual(mood, "懒洋洋")
        self.assertIn("开心", txt)


class TestResolveVoiceTag(unittest.TestCase):
    def test_mood_never_reaches_transcript(self):
        """这条最要紧：标记漏进转写文字里，用户直接看见"（语气：懒洋洋）"。"""
        out = reply.resolve_voice_tag(
            "[voice]刚下课（语气：懒洋洋）", {"voice": {"enabled": False}})
        # enabled=False 时 synth 返回空 → 退回纯文字，但标记仍要剥掉
        self.assertNotIn("语气", out)
        self.assertNotIn("[voice]", out)
        self.assertEqual(out, "刚下课")

    def test_without_voice_tag_untouched(self):
        """没标 [voice] 的普通回复一个字都不该动。"""
        t = "刚下课，外面下了点小雨（真的）"
        self.assertEqual(reply.resolve_voice_tag(t, {}), t)


class TestCacheKeyIncludesInstruction(unittest.TestCase):
    def test_different_mood_different_file(self):
        a = voice._cache_path("同一句话", "aliyun", "m", "v", "mp3", "懒洋洋")
        b = voice._cache_path("同一句话", "aliyun", "m", "v", "mp3", "有点得意")
        self.assertNotEqual(a, b,
                            "缓存 key 没带 instruction —— 换情绪会命中旧文件，"
                            "听着还是同一个声音")

    def test_same_mood_same_file(self):
        a = voice._cache_path("同一句话", "aliyun", "m", "v", "mp3", "懒洋洋")
        b = voice._cache_path("同一句话", "aliyun", "m", "v", "mp3", "懒洋洋")
        self.assertEqual(a, b, "同情绪同文本却没命中缓存，会重复花钱")

    def test_empty_instruction_still_caches(self):
        """没情绪时也要能命中，不能每次都重新合成。"""
        a = voice._cache_path("t", "aliyun", "m", "v", "mp3")
        b = voice._cache_path("t", "aliyun", "m", "v", "mp3", "")
        self.assertEqual(a, b)


class TestCatalogHasBaseline(unittest.TestCase):
    def test_aliyun_voices_all_have_instruction(self):
        """阿里音色都有基线情绪。

        没有 instruction 就是平读 —— 这是"听着假"的根因，而它不报错，
        只是听起来像播报，很容易就这么一直跑下去。
        """
        for it in voice.VOICE_CATALOG:
            if it.get("provider") != "aliyun":
                continue
            self.assertTrue((it.get("instruction") or "").strip(),
                            "%s 没配基线 instruction" % it["key"])

    def test_apply_carries_instruction(self):
        for it in voice.VOICE_CATALOG:
            if it.get("provider") != "aliyun":
                continue
            sub, _ = voice.apply({}, it["key"])
            self.assertEqual(sub.get("instruction"), it.get("instruction"),
                             "%s 切过去没带上instruction" % it["key"])

    def test_apply_clears_stale_instruction(self):
        """换到没有基线的音色（硅基）时，要删掉上一条留下的。"""
        sub, _ = voice.apply(
            {"instruction": "上一个音色的情绪"}, "sf_diana")
        self.assertNotIn("instruction", sub,
                         "旧音色的 instruction 串到新音色上了")

    def test_switch_keeps_credentials(self):
        """切音色不该把阿里凭据弄丢。

        apply 收的是整个 config（凭据在 cfg["voice"]["aliyun"]），
        不是 voice 子字典 —— 传错形状这条测试就白测了。
        """
        cfg = {"voice": {"aliyun": {"api_key": "sk-test"}}}
        sub, _ = voice.apply(cfg, "moning_01")
        self.assertEqual((sub.get("aliyun") or {}).get("api_key"), "sk-test")


class TestDraftRulesTeachesMood(unittest.TestCase):
    """她得知道有这个选项，否则永远不标，白改。"""

    def setUp(self):
        import persona_store
        self.rules = persona_store.build_draft_rules({})

    def test_mentions_mood_marker(self):
        self.assertIn("语气：", self.rules)

    def test_marker_format_is_documented(self):
        self.assertIn("（语气：xxx）", self.rules)

    def test_says_it_is_not_shown(self):
        """得说清那段不会显示出来，不然她会担心发出去很奇怪。"""
        self.assertIn("不会显示", self.rules)


if __name__ == "__main__":
    unittest.main()