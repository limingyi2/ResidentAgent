# -*- coding: utf-8 -*-
"""桌宠的远程大脑接口契约。

这里钉的是一个真实踩过的坑：pet.py 会直接调 `brain.read_history()` /
`clear_history()` / `life.catch_up()` / `life.write_diary()`，而远程大脑上
**少了哪个方法就被 except 吞成静默失效** —— 表现是"聊天窗每次启动都是空的"、
"清了记录其实没清"、"让她现在写篇日记"永远失败。所以用接口契约 + 源码断言钉住。

不构造 RemoteBrain / RemoteLife 实例（它们的 __init__ 会去连网络）。
"""
import base64
import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pet"))

import remote_brain  # noqa: E402


class TestRemoteBrainInterface(unittest.TestCase):
    """pet.py 会调到的每一个方法都必须存在。"""

    BRAIN_METHODS = ["chat", "speak_on_scene", "reset", "reload_persona",
                     "seed_history", "read_history", "clear_history"]
    LIFE_METHODS = ["list_diaries", "read_diary", "catch_up", "write_diary"]

    def test_remote_brain_exposes_every_method_pet_calls(self):
        for name in self.BRAIN_METHODS:
            self.assertTrue(hasattr(remote_brain.RemoteBrain, name),
                            "RemoteBrain 缺 %s（pet.py 会直接调）" % name)

    def test_remote_life_exposes_every_method_pet_calls(self):
        for name in self.LIFE_METHODS:
            self.assertTrue(hasattr(remote_brain.RemoteLife, name),
                            "RemoteLife 缺 %s（pet.py 会直接调）" % name)

    def test_read_history_is_a_real_call_not_a_stub(self):
        src = inspect.getsource(remote_brain.RemoteBrain.read_history)
        self.assertIn("_get", src)
        self.assertIn("/api/history", src)

    def test_clear_history_hits_the_cloud(self):
        src = inspect.getsource(remote_brain.RemoteBrain.clear_history)
        self.assertIn("/api/history/clear", src)

    def test_life_catch_up_and_write_diary_are_not_no_ops(self):
        for fn in (remote_brain.RemoteLife.catch_up,
                   remote_brain.RemoteLife.write_diary):
            src = inspect.getsource(fn)
            self.assertIn("_post", src, fn.__name__)
            self.assertIn("/api/", src, fn.__name__)


class TestTokenHandling(unittest.TestCase):
    """token 不走 URL：URL 会进各级访问日志、Referer 和浏览器历史。"""

    def test_post_sends_authorization_header(self):
        src = inspect.getsource(remote_brain.RemoteBrain._post)
        self.assertIn("Authorization", src)
        self.assertIn("Bearer ", src)

    def test_post_does_not_put_the_token_in_the_url(self):
        """老写法是 `url += sep + "token=" + quote(self.token)` —— 那会让 token
        出现在每一级访问日志里。断言的是那句拼接不存在（docstring 里提到 ?token=
        是给媒体请求留的说明，不算数）。"""
        src = inspect.getsource(remote_brain.RemoteBrain._post)
        self.assertNotIn('"token=" +', src)

    def test_life_requests_also_use_the_header(self):
        for fn in (remote_brain.RemoteLife._get, remote_brain.RemoteLife._post):
            src = inspect.getsource(fn)
            self.assertIn("Authorization", src)
            self.assertNotIn('"token=" +', src)


class TestImageEncoding(unittest.TestCase):
    """两边的 img 语义不同：本地给路径、云上要 base64。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_encodes_a_local_file(self):
        p = os.path.join(self.tmp.name, "x.bin")
        with open(p, "wb") as f:
            f.write(b"hello")
        self.assertEqual(base64.b64decode(remote_brain._as_b64(p)), b"hello")

    def test_passes_through_existing_base64(self):
        self.assertEqual(remote_brain._as_b64("aGVsbG8="), "aGVsbG8=")

    def test_empty_inputs(self):
        self.assertEqual(remote_brain._as_b64(""), "")
        self.assertEqual(remote_brain._as_b64(None), "")


if __name__ == "__main__":
    unittest.main()
