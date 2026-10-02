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
                      "应该显式清空，只把脱敏后的串放进 placeholder")

    def test_placeholder_shows_masked_key(self):
        h = self._html()
        self.assertIn("'当前：' + mCur.api_key", h,
                      "placeholder 应该显示脱敏后的当前 key，提示用户是哪一把")

    def test_empty_key_means_keep_current(self):
        """留空 = 不改。apply_cfg 里是 `if key and ...`，前端这条校验要跟它一致，
        否则用户只想改模型名也会被拦下。"""
        h = self._html()
        self.assertNotIn("!payload.api_key", h,
                         "还在要求密钥必填 —— 改别的设置时会拦下来")


if __name__ == "__main__":
    unittest.main()