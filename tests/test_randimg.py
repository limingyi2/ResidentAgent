# -*- coding: utf-8 -*-
"""随机图（[rand:分类]）这条链路。

以前这条链路**一个用例都没有**，于是"她偶尔不发表情包"能一直没人发现：
模型明明写了 [rand:bq]，拉图失败后标签被清掉，她那边就成了"啥也没发"，
用户完全看不出是图库没响应。这里把三种"发不出"分别钉住 ——
它们的用户体感完全不同，不能一律静默删标签。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.abspath(os.path.join(HERE, "..", "brain"))
sys.path.insert(0, BRAIN)

import randimg  # noqa: E402


class TestRandTagResolve(unittest.TestCase):
    """解析与降级：都不联网，靠打桩 _cloud_random_image。"""

    def setUp(self):
        self._orig = randimg._cloud_random_image
        self._calls = []

    def tearDown(self):
        randimg._cloud_random_image = self._orig

    def _stub(self, ret="rand_x.jpg", record=True):
        def f(cat):
            if record:
                self._calls.append(cat)
            return ret
        randimg._cloud_random_image = f

    def test_ok_becomes_img_tag(self):
        self._stub("p_20261002_abcd.jpg")
        out = randimg.resolve_rand_tags("给你看\n[rand:bq]", "发个表情包")
        self.assertIn("[img:p_20261002_abcd.jpg]", out)
        self.assertEqual(self._calls, ["bq"])

    def test_nested_img_rand_variant_is_normalised(self):
        # 模型常写成 [img:rand:bq]，不归一化就匹配不上，标签会原样漏给用户
        self._stub("rand_ok.jpg")
        out = randimg.resolve_rand_tags("[img:rand:bq]", "来张图")
        self.assertEqual(out, "[img:rand_ok.jpg]")

    def test_unknown_category_silently_dropped(self):
        # 分类不在册 = 规则不让发，不是故障。静默删，不解释。
        self._stub(record=True)
        out = randimg.resolve_rand_tags("好\n[rand:furry]", "发个图")
        self.assertEqual(out, "好")
        self.assertEqual(self._calls, [], "不在册的分类不该真去拉图")

    def test_wallpaper_needs_explicit_ask(self):
        self._stub("rand_wall.jpg")
        out = randimg.resolve_rand_tags("[rand:pc_wallpaper]", "来个壁纸")
        self.assertIn("[img:rand_wall.jpg]", out)

    def test_wallpaper_dropped_when_not_asked(self):
        self._stub(record=True)
        out = randimg.resolve_rand_tags("嗯\n[rand:pc_wallpaper]", "发个表情包")
        self.assertEqual(out, "嗯")
        self.assertEqual(self._calls, [])

    def test_fetch_failure_tells_user_instead_of_silent_nothing(self):
        # 这是本次修的真 bug：拉不到图时只删标签 → 她"啥也没发"，
        # 用户以为她不想发。现在必须留一句可见的说明。
        self._stub("")
        out = randimg.resolve_rand_tags("喏\n[rand:bq]", "发个表情包")
        self.assertIn("图库没响应", out)
        self.assertIn("喏", out, "正文不能被这句说明吃掉")
        self.assertNotIn("[rand:", out)
        self.assertNotIn("[img:", out)

    def test_feature_off_drops_silently_not_with_apology(self):
        # 开关是用户自己关的，说"图库没响应"等于甩锅给他
        self._stub(record=True)
        import features
        orig = features.on
        features.on = lambda k: False
        try:
            out = randimg.resolve_rand_tags("好\n[rand:bq]", "发个图")
        finally:
            features.on = orig
        self.assertEqual(out, "好")
        self.assertNotIn("图库", out)
        self.assertEqual(self._calls, [])

    def test_generated_tag_survives_cleanup(self):
        # **原 bug 的回归用例**。
        # 真实文件名就叫 rand_20261002_abcd.jpg（以 rand 开头），而旧的收尾
        # 清扫正则含 rand[^]]*，会把刚生成成功的 [img:rand_...jpg] 当残留删掉 ——
        # 于是图拉到了、标签却被自己清掉，她"啥也没发"，且必现。
        # 这个用例就是钉死它：生成出来的标签必须原样活到输出。
        self._stub("rand_20261002_abcd.jpg")
        out = randimg.resolve_rand_tags("喏\n[rand:bq]", "发个表情包")
        self.assertIn("[img:rand_20261002_abcd.jpg]", out,
                      "生成的图片标签被收尾清扫误删了 —— 这就是她从不发表情包的原因")
        self.assertNotIn("图库没响应", out, "拉图其实成功了，不该报失败")

    def test_broken_tag_before_generated_one_is_cleaned(self):
        # 残缺标签与合法标签混在一起时，只清残缺那个
        self._stub("rand_20261002_abcd.jpg")
        out = randimg.resolve_rand_tags("[rand\n[rand:bq]", "发图")
        self.assertIn("[img:rand_20261002_abcd.jpg]", out)
        self.assertNotIn("[rand", out.lower())

    def test_no_broken_tag_left_in_any_case(self):
        # 残留的 rand 标签漏到用户侧 = App 404 裂图，任何分支都不能漏。
        # 注意别写成 assertNotIn("[img:rand") —— 合法图片标签恰恰长这样
        # （[img:rand_20261002_abcd.jpg]，文件名以 rand 开头），那是**成功**的标志。
        # 这里只查两类真残留：还带分类名的（[rand:bq] / [img:rand:bq]）、
        # 以及没闭合的半截标签（[rand）。
        for stub_ret, ask in (("ok.jpg", "发图"), ("", "发图")):
            self._stub(stub_ret)
            for raw in ("[rand:bq]", "[img:rand:bq]", "[rand：bq]", "[rand:]", "[rand"):
                out = randimg.resolve_rand_tags(raw, ask)
                low = out.lower()
                self.assertNotIn("[rand:", low, raw)
                self.assertNotIn("[rand：", low, raw)
                self.assertNotIn("[img:rand:", low, raw)
                # 半截的 "[rand" 也不能留（App 会把它当标签解析失败）
                self.assertNotIn("[rand", low, raw)


class TestCloudRandomImageContract(unittest.TestCase):
    """拉图本身的契约：只认图片、失败返回空串。"""

    def setUp(self):
        self._orig_fetch = None
        import uapi
        self._uapi = uapi
        self._orig_fetch = uapi.fetch_random_image

    def tearDown(self):
        self._uapi.fetch_random_image = self._orig_fetch

    def test_returns_empty_string_on_fetch_failure(self):
        self._uapi.fetch_random_image = lambda *a, **k: None
        self.assertEqual(randimg._cloud_random_image("bq"), "")

    def test_returns_empty_string_on_exception(self):
        def boom(*a, **k):
            raise RuntimeError("network down")
        self._uapi.fetch_random_image = boom
        self.assertEqual(randimg._cloud_random_image("bq"), "")

    def test_writes_file_and_returns_name(self):
        import tempfile
        d = tempfile.mkdtemp()
        orig_upload = None
        import paths
        orig_upload = paths.UPLOAD_DIR
        paths.UPLOAD_DIR = d
        try:
            self._uapi.fetch_random_image = lambda *a, **k: b"\xff\xd8fakejpeg"
            name = randimg._cloud_random_image("bq")
            self.assertTrue(name.startswith("rand_") and name.endswith(".jpg"))
            self.assertTrue(os.path.isfile(os.path.join(d, name)))
        finally:
            paths.UPLOAD_DIR = orig_upload


if __name__ == "__main__":
    unittest.main()
