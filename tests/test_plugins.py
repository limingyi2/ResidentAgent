# -*- coding: utf-8 -*-
"""插件系统（brain/plugin_host.py + brain/plugins/）。

跑 `pytest tests/` 时会一起跑 —— 里面是 60 项断言，覆盖：装载、坏插件不拖累人、
钩子派发、工具与路由、开关、reminder 的时间解析、calc 的表达式安全。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(ROOT, ".workbuddy", "scripts", "_test_plugins.py")


class TestPluginScriptRunsClean(unittest.TestCase):
    def test_插件自测全过(self):
        """_test_plugins.py 是个独立脚本（自己造 tmpdir、不依赖 pytest），
        这里用子进程跑它，把它的 sys.exit(1) 变成一个测试失败。"""
        import subprocess
        py = os.path.join(ROOT, "venv", "Scripts", "python.exe")
        if not os.path.isfile(py):
            self.skipTest("venv 不在，跳过")
        r = subprocess.run([py, _SCRIPT], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=ROOT)
        self.assertEqual(
            r.returncode, 0,
            "插件自测没过：\n%s\n%s" % (r.stdout[-3000:], r.stderr[-1500:]))


if __name__ == "__main__":
    unittest.main()
