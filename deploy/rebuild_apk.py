# -*- coding: utf-8 -*-
r"""用 gradle 正式构建安卓 APK。

只负责 gradle 全量构建；只想换个 chat.html 快速打包，用本地工具
`.workbuddy/scripts/tools/_repackage.py`（不入库）。

路径不写死本机目录：SDK / gradle 从 ANDROID_TOOLS 环境变量取，
没有就落到默认位置，JDK 从 JAVA_HOME 取。

注意本 docstring 是 raw 字符串（前缀 r）：正文里会出现 C:\Users\... 这类
Windows 路径，普通字符串会把 \U 当转义符，整个文件直接 SyntaxError。写成
普通字符串时，路径里的反斜杠必须全部双写。
"""
import glob
import os
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "android-app"))

# Android SDK / gradle 的位置：优先读环境变量，没有就用常见默认位置
AND = (os.environ.get("ANDROID_TOOLS")
       or os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "android"))

jhs = [c for c in sorted(glob.glob(r"C:\Program Files\Java\*"))
       if os.path.exists(os.path.join(c, "bin", "java.exe"))]
if not jhs:
    raise SystemExit("没找到 JDK：请安装 JDK 或手动设置 JAVA_HOME")

env = dict(os.environ)
env["JAVA_HOME"] = os.environ.get("JAVA_HOME") or jhs[0]
env["ANDROID_HOME"] = os.path.join(AND, "sdk")
env["GRADLE_USER_HOME"] = os.path.join(AND, "gradle-home")
env["PATH"] = os.path.join(env["JAVA_HOME"], "bin") + ";" + env["PATH"]

gradle = os.path.join(AND, "gradle-8.7", "bin", "gradle.bat")
if not os.path.exists(gradle):
    raise SystemExit("没找到 gradle：请把 ANDROID_TOOLS 指到正确的目录（当前 %s）" % AND)

p = subprocess.run([gradle, "-p", ROOT, "assembleDebug", "--no-daemon"],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env, timeout=500)
print("RC", p.returncode)

apk = os.path.join(ROOT, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
if os.path.exists(apk):
    dst = os.path.join(HERE, "zhixia.apk")
    shutil.copyfile(apk, dst)
    print("APK", os.path.getsize(dst), dst)
else:
    print("NO APK")
    print((p.stdout or "")[-400:])
