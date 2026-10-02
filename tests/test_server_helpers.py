# -*- coding: utf-8 -*-
"""服务端里那些纯函数：鉴权判定、回环判断、trace 快照、查询参数。

鉴权这块以前有个真实的口子：**没配 token 时一律放行**（fail-open），
配上文档推荐的 bind_host: 0.0.0.0 就等于把整个 API 摆到公网。所以它有专门的测试。
"""
import inspect
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import server  # noqa: E402


class TestLoopback(unittest.TestCase):
    def test_loopback_addresses(self):
        for ip in ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "LOCALHOST"]:
            self.assertTrue(server._is_loopback(ip), repr(ip))

    def test_non_loopback_addresses(self):
        for ip in ["0.0.0.0", "47.114.58.45", "192.168.1.5", "10.0.0.1",
                   "", None, "example.com"]:
            self.assertFalse(server._is_loopback(ip), repr(ip))


class TestAuth(unittest.TestCase):
    TOK = "s3cret-token-value"

    def test_correct_token_in_header(self):
        self.assertTrue(server._auth_ok("Bearer " + self.TOK, "", self.TOK,
                                        "47.114.58.45"))

    def test_correct_token_in_query_still_accepted(self):
        """媒体 URL（<img>/<audio>）设不了请求头，只能走 ?token=，必须继续认。"""
        self.assertTrue(server._auth_ok("", self.TOK, self.TOK, "47.114.58.45"))

    def test_wrong_token_rejected(self):
        for bad in ["", "nope", self.TOK[:-1], self.TOK + "x", "Bearer",
                    "Bearer nope"]:
            self.assertFalse(
                server._auth_ok(bad, "", self.TOK, "127.0.0.1"), repr(bad))

    def test_missing_token_rejected_when_one_is_configured(self):
        self.assertFalse(server._auth_ok("", "", self.TOK, "127.0.0.1"))

    def test_no_configured_token_only_allows_loopback(self):
        """这是那个 fail-open 口子的回归测试。"""
        self.assertTrue(server._auth_ok("", "", "", "127.0.0.1"))
        self.assertTrue(server._auth_ok("", "", None, "::1"))
        self.assertFalse(server._auth_ok("", "", "", "47.114.58.45"))
        self.assertFalse(server._auth_ok("", "", "", "192.168.1.9"))

    def test_uses_constant_time_comparison(self):
        src = inspect.getsource(server._auth_ok)
        self.assertIn("compare_digest", src)
        self.assertNotIn('== str(config_token)', src)


class TestQueryParam(unittest.TestCase):
    def test_reads_value(self):
        self.assertEqual(server._query_param("/api/trace?last=20", "last"), "20")
        self.assertEqual(
            server._query_param("/api/img?name=a.png&x=1", "name"), "a.png")

    def test_missing_key_and_garbage(self):
        self.assertEqual(server._query_param("/api/trace", "last"), "")
        self.assertEqual(server._query_param("", "last"), "")


class TestSnapshot(unittest.TestCase):
    """_snapshot 必须在**持锁时**调用 —— 它抄的是共享对象上的那几个字段。"""

    def _brain(self):
        b = types.SimpleNamespace()
        b.mem = types.SimpleNamespace(
            last_hits=[(0.71, "记忆甲"), (0.64, "记忆乙"), (0.55, "记忆丙")])
        b.last_sizes = {"persona": 512, "memory": 180, "total": 3210}
        b.last_ms = 1620
        b.last_usage = {"in": 3210, "out": 74}
        return b

    def test_copies_all_the_trace_fields(self):
        snap = server._snapshot(self._brain())
        self.assertEqual(snap["mem_hits"], 3)
        self.assertEqual(snap["mem_top"], [0.71, 0.64, 0.55])
        self.assertEqual(snap["blocks"]["persona"], 512)
        self.assertEqual(snap["ms"], 1620)
        self.assertEqual(snap["tok_in"], 3210)
        self.assertEqual(snap["tok_out"], 74)
        self.assertIn("model", snap)

    def test_tolerates_a_brain_missing_everything(self):
        snap = server._snapshot(object())
        self.assertEqual(snap["mem_hits"], 0)
        self.assertEqual(snap["mem_top"], [])
        self.assertEqual(snap["blocks"], {})
        self.assertIsNone(snap["ms"])
        self.assertIsNone(snap["tok_in"])

    def test_handles_brain_that_never_talked(self):
        b = types.SimpleNamespace(mem=types.SimpleNamespace(last_hits=None),
                                  last_sizes=None, last_ms=None,
                                  last_usage=None)
        snap = server._snapshot(b)
        self.assertEqual(snap["mem_hits"], 0)
        self.assertEqual(snap["blocks"], {})


if __name__ == "__main__":
    unittest.main()
