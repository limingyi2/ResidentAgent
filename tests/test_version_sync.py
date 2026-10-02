# -*- coding: utf-8 -*-
"""版本号必须只有一个真源。

以前版本号写在四个地方：build.gradle 的 versionCode、chat.html 的 APP_CODE、
PollService 的 installedCode、云端 app/version.json。四处各写各的 ——
手机上装的是 22、云端说 23，装完照样弹更新提示，能一直弹到天荒地老
（真实发生过，见 2026-10-03）。

现在全从仓库根的 version.json 派生。这几条钉住那个约定。
"""
import io
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with io.open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


class TestSingleSourceOfVersion(unittest.TestCase):
    def setUp(self):
        self.ver = json.loads(_read("version.json"))

    def test_version_json_字段齐全(self):
        for k in ("code", "name", "min_sdk", "target_sdk"):
            self.assertIn(k, self.ver, k)
        self.assertIsInstance(self.ver["code"], int)
        self.assertGreater(self.ver["code"], 0)

    def test_build_gradle_不再写死版本(self):
        g = _read("android-app/app/build.gradle")
        self.assertNotRegex(g, r"versionCode\s+\d",
                            "versionCode 写死了 —— 应该从 version.json 读")
        self.assertNotRegex(g, r'versionName\s+"',
                            "versionName 写死了 —— 应该从 version.json 读")
        self.assertIn("version.json", g, "gradle 应该读 version.json")

    def test_pollservice_不再写死installedCode(self):
        # 写死 int 的话，发版漏改这里就变成"装完还提示"
        j = _read("android-app/app/src/main/java/com/resident/chat/PollService.java")
        self.assertNotRegex(j, r"int\s+installedCode\s*=\s*\d")
        self.assertIn("BuildConfig.VERSION_CODE", j)

    def test_chat_html_的APP_CODE是占位(self):
        # 仓库里必须是 0，由 rebuild_apk.py 从 version.json 注入
        h = _read("android-app/app/src/main/assets/chat.html")
        m = re.search(r"const\s+APP_CODE\s*=\s*(\d+)", h)
        self.assertIsNotNone(m, "找不到 APP_CODE")
        self.assertEqual(m.group(1), "0",
                         "仓库版 APP_CODE 应该是 0（构建时注入），不是手写的号")

    def test_打包脚本会注入版本号(self):
        s = _read("deploy/rebuild_apk.py")
        self.assertIn("APP_CODE", s, "打包时没注入 APP_CODE")
        self.assertIn("version.json", s)
        # 注入后必须还原：仓库那份要一直是空占位
        self.assertIn("还原", s, "注入后没还原，仓库那份会脏")

    def test_两份chat_html一致(self):
        # 版本号占位符改动如果只改了一份，打包出来的和仓库里的会对不上
        a = _read("_chat.html")
        b = _read("android-app/app/src/main/assets/chat.html")
        self.assertEqual(a, b, "_chat.html 与 assets 那份不一致")


if __name__ == "__main__":
    unittest.main()
