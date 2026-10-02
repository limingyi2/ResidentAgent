# -*- coding: utf-8 -*-
"""密钥不能明文下发到客户端。

背景：模型设置页要显示"现在用的是哪把密钥"，但密钥是账号级凭据 ——
明文走 HTTP 等于任何人抓到一次包、或看到一次屏幕就能拿去刷额度。
`model_hub.current()` 返回的 `api_key` 是**完整明文**（服务端自己要用它调
服务商），所以回给客户端必须走 `public_current()`。

这条一旦被改回去不会有任何报错，只会在用户截图或抓包时泄出去，
所以用测试钉住。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import model_hub  # noqa: E402

# 拿一个足够长的假 key，短的会走另一条分支
FAKE = "sk-fake000000000000000000000000000000000000000000000000"


def cfg_with_key(k=FAKE):
    return {
        "provider": "siliconflow",
        "api_base": "https://example.invalid/v1",
        "api_key": k,
        "model": "some-chat-model",
    }


class TestCurrentKeepsPlainKey(unittest.TestCase):
    def test_current_is_still_plain(self):
        """服务端自己调服务商要真 key —— current() 不能被顺手改成脱敏的，
        否则 /api/models 拉不到模型列表（而且不会报错，只显示"共 0 个"）。"""
        self.assertEqual(model_hub.current(cfg_with_key())["api_key"], FAKE)


class TestPublicCurrentMasksKey(unittest.TestCase):
    def test_key_is_not_returned_in_full(self):
        got = model_hub.public_current(cfg_with_key())["api_key"]
        self.assertNotIn(FAKE, got, "完整密钥出现在给客户端的响应里")
        self.assertNotEqual(got, FAKE)

    def test_still_shows_enough_to_recognise(self):
        """要能认出是哪一把，不然用户不知道当前用的是哪个。"""
        got = model_hub.public_current(cfg_with_key())["api_key"]
        self.assertIn(FAKE[:6], got)
        self.assertIn(FAKE[-4:], got)

    def test_short_key_is_fully_hidden(self):
        """短 key 没有"前后各几位"可言，全盖掉。"""
        got = model_hub.public_current(cfg_with_key("sk-abc"))["api_key"]
        self.assertEqual(got, "******")

    def test_empty_key_does_not_crash(self):
        d = model_hub.public_current({"api_key": ""})
        self.assertEqual(d["api_key"], "")

    def test_other_fields_survive(self):
        d = model_hub.public_current(cfg_with_key())
        self.assertEqual(d["provider"], "siliconflow")
        self.assertEqual(d["chat"], "some-chat-model")
        self.assertEqual(d["api_base"], "https://example.invalid/v1")


class TestServerNeverSendsPlainKey(unittest.TestCase):
    """server.py 里凡是回给客户端的 current，必须是脱敏版。

    唯一该出现 current() 的地方是 `/api/models` 里拿 key 去调服务商 ——
    那次调用结束后不能再把它放进响应。
    """

    def _src(self):
        import server
        return inspect_source(server)

    def test_no_public_current_call_sends_plain(self):
        import server
        src = inspect_source(server)
        # 把 current( 直接放进响应的那种写法全找出来
        bad = [ln.strip() for ln in src.splitlines()
               if '"current": model_hub.current(' in ln
               or "'current': model_hub.current(" in ln]
        self.assertEqual(bad, [],
                         "响应里用了 current() 而不是 public_current()：%s" % bad)


def inspect_source(mod):
    import inspect
    try:
        return inspect.getsource(mod)
    except Exception:
        import io
        with io.open(mod.__file__, encoding="utf-8") as f:
            return f.read()


class TestFrontendNeverPrefillsKey(unittest.TestCase):
    """前端不能把服务端回传的 key 填进输入框 —— 那是最后一道防线。

    输入框里的东西肉眼可见、随手可截图。就算服务端哪天又明文返回了，
    前端这一处也不该把它显示出来。
    """

    def _html(self):
        import io
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(root, "android-app", "app", "src", "main",
                         "assets", "chat.html")
        if not os.path.exists(p):
            self.skipTest("assets/chat.html 不在（仓库里可能省略）")
        with io.open(p, encoding="utf-8") as f:
            return f.read()

    def test_mkey_value_is_never_set_from_server(self):
        h = self._html()
        self.assertNotIn("$('mKey').value = mCur.api_key", h,
                         "把服务端回的 key 填进了输入框")
        self.assertIn("$('mKey').value = ''", h,
                      "应该显式清空")

    def test_placeholder_never_shows_the_masked_key(self):
        """placeholder 里出现打码串是有害的：它就在输入框里，手一抖复制重贴回去
        就会把真 key 覆盖掉。改成只说"已有一把"，靠 api_key_set 判。"""
        h = self._html()
        self.assertNotIn("'当前：' + mCur.api_key", h,
                         "placeholder 不该显示打码后的 key")
        self.assertIn("api_key_set", h,
                      "应该用 api_key_set 判断有没有那把")

    def test_empty_key_means_keep_current(self):
        """留空 = 不改。apply_cfg 里是 `if key and not is_masked(key)`，
        前端这条校验要跟它一致，否则用户只想改模型名也会被拦下。"""
        h = self._html()
        self.assertNotIn("!payload.api_key", h,
                         "还在要求密钥必填 —— 改别的设置时会拦下来")


class TestMaskedKeyNotWrittenBack(unittest.TestCase):
    """打码串回传时不能覆盖真 key —— 这是上一条的下游后果。

    `apply_cfg` 原来只判 `key != cfg["api_key"]`，打码串确实不等于真 key，
    于是它会作为"用户换了新密钥"被写进去，写完 AI 一句也说不出来。
    前端只要不小心回填一次（复制粘贴、浏览器 autofill、老版本压根就是预填的）
    就中招，所以这一层必须在服务端堵，不能只靠前端不填。
    """

    REAL = "sk-real0000000000000000000000000000000000000000"

    def _cfg(self):
        return cfg_with_key(self.REAL)

    def _apply(self, key):
        cfg = self._cfg()
        new, changed = model_hub.apply_cfg(
            cfg, {"api_base": cfg["api_base"], "api_key": key, "chat": "m2"})
        return new, changed

    def test_打码串回传不覆盖真key(self):
        masked = model_hub.mask_key(self.REAL)
        new, _ = self._apply(masked)
        self.assertEqual(new["api_key"], self.REAL, "真 key 被打码串覆盖了")

    def test_三种打码形态都挡(self):
        for k in (model_hub.mask_key(self.REAL), "sk-real...0000", "********"):
            new, changed = self._apply(k)
            self.assertEqual(new["api_key"], self.REAL, k)
            self.assertTrue(any("没改" in c for c in changed),
                            "应该明说密钥没改，而不是静默吞掉：%s" % changed)

    def test_真key仍能正常更新(self):
        # 堵住打码串不能变成"什么都不许写" —— 用户换密钥是正常功能
        new, changed = self._apply("sk-brand-new-key-000000000000")
        self.assertEqual(new["api_key"], "sk-brand-new-key-000000000000")
        self.assertIn("密钥已更新", changed)

    def test_空串仍是沿用当前(self):
        new, changed = self._apply("")
        self.assertEqual(new["api_key"], self.REAL)
        self.assertNotIn("密钥已更新", changed)
        self.assertFalse(any("没改" in c for c in changed),
                         "空串是正常的\"沿用\"，不该报成异常")

    def test_is_masked_判定(self):
        self.assertTrue(model_hub.is_masked("sk-a…cdef"))
        self.assertTrue(model_hub.is_masked("sk-a...cdef"))
        self.assertTrue(model_hub.is_masked("****"))
        self.assertFalse(model_hub.is_masked("sk-REAL"))
        self.assertFalse(model_hub.is_masked(""))
        self.assertFalse(model_hub.is_masked(None))

    def test_public_current_给api_key_set(self):
        d = model_hub.public_current(self._cfg())
        self.assertTrue(d["api_key_set"])
        self.assertFalse(model_hub.public_current(cfg_with_key(""))["api_key_set"])

    def test_public_current_仍不泄露明文(self):
        # 加 api_key_set 之后别忘了脱敏本身 —— 两条断言都要在
        d = model_hub.public_current(self._cfg())
        self.assertNotEqual(d["api_key"], self.REAL)
        self.assertIn("…", d["api_key"])


if __name__ == "__main__":
    unittest.main()