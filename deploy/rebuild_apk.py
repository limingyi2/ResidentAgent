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
dst = os.path.join(HERE, "zhixia.apk")

# 构建失败时必须先把错误打出来再退出。之前这里只在"APK 文件不存在"时才
# 打印 stdout，而 APK 是上一次成功留下的旧文件 —— 于是失败也照样打印一行
# "APK 256937 ..."，看着像成功了。gradle 的中文报错在 javac 那段，
# 只看结尾几行还会以为是别的问题。
if p.returncode != 0:
    out = p.stdout or ""
    # 错误在最后 60 行里，javac 的中文信息在这里面
    tail = out[-3000:] if out else "(gradle 没有 stdout)"
    print("构建失败，最后 3000 字输出：")
    print(tail)
    if p.stderr:
        print("stderr:", p.stderr[-1000:])
    raise SystemExit("gradle assembleDebug 失败（RC %d），没生成新 APK" % p.returncode)

if os.path.exists(apk):
    shutil.copyfile(apk, dst)
    print("APK", os.path.getsize(dst), dst)
else:
    print("NO APK")
    print((p.stdout or "")[-400:])
    raise SystemExit("gradle 说成功了但找不到 APK，产物路径变了？")
