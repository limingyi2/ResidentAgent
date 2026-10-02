# -*- coding: utf-8 -*-
"""前端与工程约定的不变量。不需要浏览器，纯静态检查。

这里钉的是 README 第八节自己定的规矩，以及几个已经踩过的坑：
- 根目录 `_chat.html`（前端母本）和 `assets/chat.html` 内容必须一致，
  否则"本地改好了、装到手机上还是旧的"；
- `esc()` 被用在 `data-mid="…"` / `src="…"` 这类**属性**里，必须转义引号；
- 同一个作用域里不许再出现两份 `copyText`（后者会静默覆盖前者）。
"""
import hashlib
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET_HTML = os.path.join(ROOT, "android-app", "app", "src", "main",
                          "assets", "chat.html")
ROOT_HTML = os.path.join(ROOT, "_chat.html")


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _src():
    with open(ASSET_HTML, encoding="utf-8") as f:
        return f.read()


class TestFrontendInvariants(unittest.TestCase):

    def test_asset_html_exists(self):
        self.assertTrue(os.path.isfile(ASSET_HTML))

    def test_the_two_copies_are_byte_identical(self):
        """母本被 gitignore，克隆下来本来就没有 → 那种情况下跳过。"""
        if not os.path.isfile(ROOT_HTML):
            self.skipTest("根目录 _chat.html 不在（它不入库，克隆后本就没有）")
        self.assertEqual(_sha256(ROOT_HTML), _sha256(ASSET_HTML),
                         "_chat.html 与 assets/chat.html 不一致："
                         "改前端必须两份一起改")

    def test_esc_escapes_quotes_for_attribute_contexts(self):
        src = _src()
        esc = re.search(r"function esc\(s\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(esc, "找不到 esc()")
        body = esc.group(1)
        self.assertIn("&quot;", body, "esc() 不转义双引号，属性里会被突破")
        self.assertIn("&#39;", body, "esc() 不转义单引号")

    def test_esc_is_used_in_attribute_position(self):
        """确认上面那条不是多余要求：它真的被用在属性里。"""
        self.assertIn("""data-mid="' + esc(""", _src())

    def test_exactly_one_copyText_definition(self):
        self.assertEqual(_src().count("function copyText"), 1,
                         "copyText 又被定义了两份（后者会静默覆盖前者）")

    def test_the_old_duplicate_helper_is_gone(self):
        """copyTextCompat 已合并进 copyText（名字出现在说明注释里是允许的）。"""
        self.assertNotIn("function copyTextCompat", _src())

    def test_api_calls_carry_an_auth_header(self):
        src = _src()
        self.assertIn("Authorization", src)
        self.assertIn("Bearer ", src)

    def test_fetch_wrapper_strips_the_token_from_the_url(self):
        """头能带了，URL 里那份就该去掉 —— 它会进访问日志和 Referer。"""
        src = _src()
        wrapper = re.search(r"window\.fetch = function.*?\n\};", src, re.S)
        self.assertIsNotNone(wrapper, "找不到 fetch 包装")
        self.assertIn("token=", wrapper.group(0))
        self.assertIn("Authorization", wrapper.group(0))

    def test_all_python_and_json_config_is_valid_utf8(self):
        for rel in ("brain/config/config.example.json",
                    "README.md", "android-app/app/src/main/assets/chat.html"):
            p = os.path.join(ROOT, rel)
            with open(p, encoding="utf-8") as f:
                f.read()


if __name__ == "__main__":
    unittest.main()
