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


class TestSheetLayers(unittest.TestCase):
    """自绘弹层：全局只允许存在一个，且必须能被关掉。

    踩过的坑：长按表情包时 Android WebView 会**同时**发 touchstart(550ms 定时器)
    与 contextmenu，两者都调了弹层函数 → body 上叠两层 position:fixed 遮罩 →
    点"取消"只关掉最上面那层，底下那层原地不动 → 整个 App 点哪都没反应，
    按返回键回主菜单也没用（遮罩挂在 body 上）。
    """

    def test_sheet_registry_exists(self):
        src = _src()
        self.assertIn("let curSheet", src, "缺全局弹层引用，新弹层会盖住旧的")
        self.assertIn("function closeSheet", src)
        self.assertIn("function newSheet", src)

    def test_every_sheet_goes_through_the_registry(self):
        """所有建遮罩的地方都必须走 newSheet/closeSheet，不能自己 createElement。

        这是防复发的关键：以前四处各建各的，谁也不认识谁，重复触发就叠层。
        """
        src = _src()
        # 遮罩那个固定 cssText 只该在 newSheet / pkgEditNote 里出现
        hits = [m.start() for m in re.finditer(r"position:fixed;inset:0;background:rgba", src)]
        self.assertLessEqual(
            len(hits), 2,
            "出现了第 %d 处各自建遮罩 —— 必须统一走 newSheet/closeSheet，"
            "否则重复触发又会叠层" % len(hits))

    def test_back_button_closes_the_sheet_first(self):
        """返回键必须先关弹层 —— 万一还有漏网的遮罩，这是唯一出路。"""
        src = _src()
        m = re.search(r"function androidBack\(\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "找不到 androidBack")
        body = m.group(1)
        self.assertIn("curSheet", body)
        self.assertLess(body.index("curSheet"), body.index("viewer"),
                        "弹层要先于其他浮层关闭（它浮在最上面）")

    def test_long_press_fires_only_once(self):
        """长按必须只触发一次回调。

        touchstart 定时器与 contextmenu 都会到，缺了去重就会弹两层。
        """
        src = _src()
        m = re.search(r"function addLongPress\(el, fn\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "找不到 addLongPress")
        body = m.group(1)
        self.assertIn("fired", body, "长按没有去重标志 —— 长按一次会弹两个弹层")
        self.assertIn("touchcancel", body, "漏了 touchcancel，手指被系统打断时定时器不会清")

    def test_no_leftover_direct_overlay_removal(self):
        """弹层内部的按钮不该再直接 ov.remove()，要统一走 closeSheet()。

        直接 remove 的话 curSheet 还指着那个已摘掉的节点，
        返回键会以为还有弹层、closeSheet 去摘一个已经不在的节点（空转，表现为按键无效）。
        """
        src = _src()
        self.assertNotIn("ov.remove()", src,
                         "还有地方直接 ov.remove()，统一改成 closeSheet()")


if __name__ == "__main__":
    unittest.main()
