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
import subprocess
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


class TestNoHardcodedIdentity(unittest.TestCase):
    """仓库是公开的：任何"具体角色"和"具体地点"都不能写死在代码里。

    上一轮脱敏只扫了中文，漏掉了拼音 —— 用拼音写的角色 ID、用户 ID、
    籍贯照样能看出角色是谁，籍贯更是直接写了城市。
    这类测试的价值在于：以后再写死一个新人设/新地名，会当场被拦住。

    写这类测试时不要在注释或断言里写出真实的拼音例子 —— 那等于把要遮的
    东西又抄了一份进公开仓库。这里的黑名单词全部在运行时拼出来
    （见 _FORBIDDEN_PY），例子里只用占位符。
    """

    # 拼音与包名黑名单。
    #
    # 这里必须用码点拼，不能把词拆成两段字面量再拼 —— 字符串在源文件里本来
    # 就是连续写下来的，拆开拼照样留下连续的字符，等于把要遮的词又抄了一份。
    # chr() 出来的字符在源码里只是一串数字，人搜不到。
    #
    # C:\linzhixia\ 是云端部署路径、代码逻辑依赖，不算命中，所以不查全仓。
    # 同理 README 里提到的私钥文件名也是部署路径，不是身份信息。
    @staticmethod
    def _zhixia():
        """角色拼音，用码点拼 —— 源码里不留连续字符。"""
        return chr(122) + chr(104) + chr(105) + chr(120) + chr(105) + chr(97)

    @staticmethod
    def _forbidden_pinyin():
        Z = TestNoHardcodedIdentity._zhixia()
        return (chr(108) + chr(105) + chr(110) + Z,          # lin+拼音
                Z + "_friend",                                # 拼音+_friend
                Z + "_",                                      # 拼音+_
                "com." + Z, "com/" + Z)

    def test_pinyin_identity_not_hardcoded(self):
        for rel in ("_chat.html",
                    "android-app/app/src/main/assets/chat.html"):
            p = os.path.join(ROOT, rel)
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                src = f.read()
            for w in self._forbidden_pinyin():
                self.assertNotIn(w, src, "%s 里还写死了拼音身份" % rel)

    def test_no_test_file_contains_pinyin_identity(self):
        """测试文件自己也不许出现真实拼音。

        写"不许写死身份"的测试时，很容易在注释里举个真实例子当反例、
        或把词抄进断言列表 —— 那等于把要遮的东西又公开一遍。
        这里的词全部由 chr() 拼出，源码里只是一串数字。

        唯一允许的例外是**云端部署路径**（形如盘符 + 反斜杠 + 拼音目录）。
        那是代码逻辑依赖、删了功能会坏，不是身份信息。
        """
        # 命中后只放行"部署路径"形态：盘符 + 路径分隔符 + <目录名> + 分隔符。
        # 目录名要跟黑名单里的完整形态一致（黑名单查的是 lin+拼音，这里也得用
        # 同一个词），否则放行列表匹配不上，等于没放行
        # 不用正则 —— 路径分隔符在正则里是转义符，拼出来总出岔子；
        # 这里按字面量替换，更直白。
        sep = chr(92)          # 反斜杠
        dep_dir = chr(108) + chr(105) + chr(110) + self._zhixia()   # 部署目录名
        dep_paths = tuple(d + sep + dep_dir + e
                          for d in ("C:", "F:") for e in (sep, "/"))
        tdir = os.path.join(ROOT, "tests")
        for fn in sorted(os.listdir(tdir)):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(tdir, fn)
            with open(p, encoding="utf-8") as f:
                src = f.read()
            for w in self._forbidden_pinyin():
                # 先把合法的部署路径挖掉，剩下的才是真泄露
                rest = src
                for dp in dep_paths:
                    rest = rest.replace(dp, "<DEPLOY_PATH>")
                self.assertNotIn(
                    w, rest,
                    "%s 里出现了拼音身份字面量（黑名单要用 chr() 拼，"
                    "别把词抄进注释或断言）" % fn)

    def test_android_package_is_not_role_pinyin(self):
        """包名会出现在 GitHub 路径、APK 文件名里，等于公开的角色名。"""
        g = os.path.join(ROOT, "android-app", "app", "build.gradle")
        with open(g, encoding="utf-8") as f:
            gradle = f.read()
        self.assertNotIn(self._zhixia(), gradle, "build.gradle 的包名/命名空间还带角色名")
        # namespace 与 applicationId 必须一致，否则 gradle 直接构建失败
        m = re.search(r"namespace\s+'([^']+)'", gradle)
        a = re.search(r'applicationId\s+"([^+"]+)"', gradle)
        self.assertIsNotNone(m, "读不到 namespace")
        self.assertIsNotNone(a, "读不到 applicationId")
        self.assertEqual(m.group(1), a.group(1), "namespace 与 applicationId 不一致")

    def test_java_package_matches_gradle(self):
        """java 目录结构、package 声明、build.gradle 三处必须一致。"""
        g = os.path.join(ROOT, "android-app", "app", "build.gradle")
        with open(g, encoding="utf-8") as f:
            ns = re.search(r"namespace\s+'([^']+)'", f.read()).group(1)
        expect_dir = os.path.join(ROOT, "android-app", "app", "src", "main",
                                  "java", *ns.split("."))
        self.assertTrue(os.path.isdir(expect_dir),
                        "包名 %s 对应的源码目录不存在：%s" % (ns, expect_dir))
        for fn in os.listdir(expect_dir):
            if fn.endswith(".java"):
                with open(os.path.join(expect_dir, fn), encoding="utf-8") as f:
                    first = f.readline().strip()
                self.assertEqual(first, "package %s;" % ns,
                                 "%s 的 package 声明与 build.gradle 不一致" % fn)

    def test_manifest_activity_matches_package(self):
        """AndroidManifest 里的组件名必须落在当前命名空间下。

        两种写法都得认：相对名 `.MainActivity`（AGP 按 namespace 拼）和全限定名。
        这个测试原来只匹配 `com.` 开头的全限定名，而这份 Manifest 用的是相对名，
        于是 findall 返回空、循环一次都没跑，测试一直"绿"着但什么都没断言。
        （这正是"测试看着像保护、实际是空转"的那种坑，和空 token 全放行同一类。）
        """
        mf = os.path.join(ROOT, "android-app", "app", "src", "main",
                          "AndroidManifest.xml")
        with open(mf, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn(self._zhixia(), src, "Manifest 里还有角色名")

        g = os.path.join(ROOT, "android-app", "app", "build.gradle")
        with open(g, encoding="utf-8") as f:
            ns = re.search(r"namespace\s+'([^']+)'", f.read()).group(1)

        comps = re.findall(
            r"<(?:activity|service|receiver|provider)\b[^>]*?"
            r'android:name="([^"]+)"', src, re.S)
        self.assertTrue(comps, "一个组件都没解析到 —— 正则失效，测试又在空转")
        for name in comps:
            if name.startswith("."):
                resolved = ns + name
            elif "." not in name:
                resolved = ns + "." + name
            else:
                resolved = name
            self.assertTrue(
                resolved.startswith(ns + "."),
                "Manifest 组件 %s 解析成 %s，不在命名空间 %s 下"
                % (name, resolved, ns))

        # 组件名改了、java 文件没跟着改的话运行期才会崩 —— 顺手核一下类文件在不在
        java_dir = os.path.join(ROOT, "android-app", "app", "src", "main",
                                "java", *ns.split("."))
        for name in comps:
            cls = name.rsplit(".", 1)[-1]
            self.assertTrue(
                os.path.isfile(os.path.join(java_dir, cls + ".java")),
                "Manifest 声明的 %s 找不到对应的 %s.java" % (name, cls))

    def test_no_hardcoded_region_in_profile_page(self):
        """资料页的地区不能写死 —— 换人设后它会露出上一个角色的籍贯。"""
        src = _src()
        m = re.search(r'<div class="pr"[^>]*>ID：([^<]*)</div>', src)
        self.assertIsNotNone(m, "找不到资料页的 ID 行")
        # 允许占位（her / me），但不许出现"地区：XXX"这种具体地名
        self.assertNotIn("地区：", src, "资料页还写死了地区")
        self.assertIn("data-her-id", src,
                      "ID 没走 data-her-id，人设名变了它不会跟着变")


class TestServiceAddrNotCommitted(unittest.TestCase):
    """仓库版 chat.html 必须保持空占位 —— 地址和口令是构建时注入的。

    背景：换安卓包名等于装了个"全新 App"，localStorage 是空的，而
    `DEF_BASE` 是空串时每个 fetch 都把 "/api/xxx" 当本地相对路径，
    必然失败、用户只看到"连接不到网络"。所以本地打包时会用
    `brain/config/app_conn.json`（gitignore）把真实地址注入 APK。
    **注入只发生在构建那一刻**，仓库里这份必须还是空的 ——
    否则云机公网 IP 和 brain_token 就跟着进公开仓库了。
    """

    def test_def_base_is_empty_placeholder(self):
        m = re.search(r"const DEF_BASE\s*=\s*'([^']*)'", _src())
        self.assertIsNotNone(m, "找不到 DEF_BASE 占位串")
        self.assertEqual(m.group(1), "",
                         "仓库版 chat.html 里 DEF_BASE 被写成了真实地址，"
                         "云机 IP 会跟着进公开仓库；真实地址应由 "
                         "deploy/rebuild_apk.py 在构建时注入")

    def test_def_token_is_empty_placeholder(self):
        m = re.search(r"const DEF_TOKEN\s*=\s*'([^']*)'", _src())
        self.assertIsNotNone(m, "找不到 DEF_TOKEN 占位串")
        self.assertEqual(m.group(1), "",
                         "仓库版 chat.html 里 DEF_TOKEN 被写成了真实口令")

    def test_no_public_ip_or_brain_token_in_html(self):
        """再兜一层：不管写成什么形式，HTML 里不该出现 IPv4 或长十六进制口令。"""
        src = _src()
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", src):
            self.assertTrue(ip.startswith("203.0.113."),
                            "chat.html 里出现真实 IP：%s" % ip)
        # brain_token 是 12 位小写十六进制；doc 里也提过云端地址，一并排除
        for tok in re.findall(r"\b[0-9a-f]{12}\b", src):
            self.fail("chat.html 里出现疑似 brain_token：%s" % tok)

    def test_app_conn_is_gitignored(self):
        """带云机 IP 和口令的注入源文件绝不能入库。"""
        conn = os.path.join(ROOT, "brain", "config", "app_conn.json")
        if not os.path.isfile(conn):
            self.skipTest("本机还没有 app_conn.json（没打过包）")
        r = subprocess.run(["git", "check-ignore", "-q", conn], cwd=ROOT)
        self.assertEqual(r.returncode, 0,
                         "brain/config/app_conn.json 没被 gitignore —— "
                         "它里面有云机公网 IP 和 brain_token")

    def test_empty_base_is_reported_not_silent(self):
        """没填地址时必须有明确提示，不能静默失败。

        静默失败时用户看到的是 "Failed to fetch"，翻译成人话就是
        "网络不通" —— 会往手机网络、代理、服务器上找半天，
        而真实原因（地址没填）根本不在这些地方。
        """
        src = _src()
        self.assertIn("function needSetup(", src,
                      "缺 needSetup()：地址为空时会静默失败")
        m = re.search(r"function needSetup\([^)]*\)\s*\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "needSetup 函数体解析不了")
        body = m.group(1)
        self.assertIn("errLocal(", body, "地址为空时要记一笔，否则日志里看不到原因")
        self.assertIn("settings", body, "地址为空时要把人送到设置页")


if __name__ == "__main__":
    unittest.main()
