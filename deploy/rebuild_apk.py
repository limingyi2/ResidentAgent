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

# --- 构建前把服务地址/口令注入 assets/chat.html，构建完还原 -------------
# 为什么必须注入而不是让用户手填：改安卓包名等于装了一个"全新 App"，Android
# 会给它一份空的 localStorage，而 chat.html 的 DEF_BASE 是空串 —— 于是每个
# fetch 都把 "/api/xxx" 当成本地相对路径去 file:/// 下找，必然失败，用户只看到
# "连接不到网络"。让人每次换包名都手动重填一次地址，既烦又容易填错。
# 注入只在本地构建时发生，仓库里那份 chat.html 始终是空占位（不能泄露地址/口令）。
CONN = os.path.join(os.path.dirname(ROOT), "brain", "config", "app_conn.json")
ASSET = os.path.join(ROOT, "app", "src", "main", "assets", "chat.html")
backup = None
if os.path.exists(CONN):
    import json
    conn = json.load(open(CONN, encoding="utf-8"))
    base = str(conn.get("base") or "").strip().rstrip("/")
    token = str(conn.get("token") or "").strip()
    if base and token:
        src = open(ASSET, encoding="utf-8", newline="").read()
        backup = src
        out = src.replace("const DEF_BASE  = '';", "const DEF_BASE  = '%s';" % base, 1)
        out = out.replace("const DEF_TOKEN = '';", "const DEF_TOKEN = '%s';" % token, 1)
        if out == src:
            print("!! 没找到 DEF_BASE/DEF_TOKEN 占位串，注入没生效（chat.html 结构变了？）")
        else:
            open(ASSET, "w", encoding="utf-8", newline="").write(out)
            print("已注入服务地址:", base)
    else:
        print("app_conn.json 里 base/token 为空，跳过注入（用户需手填一次）")
else:
    print("没有 %s，跳过注入（用户需在 App 设置里手填一次）" % CONN)

try:
    p = subprocess.run([gradle, "-p", ROOT, "assembleDebug", "--no-daemon"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=500)
finally:
    # 还原：仓库里那份必须保持空占位，否则 git diff 会脏、地址/口令可能进仓库
    if backup is not None:
        open(ASSET, "w", encoding="utf-8", newline="").write(backup)
        print("已还原 assets/chat.html（仓库版本保持空占位）")
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
