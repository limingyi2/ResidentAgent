# -*- coding: utf-8 -*-
import os, glob, subprocess, shutil
ROOT = r"C:\Users\limingyi\WorkBuddy\2026-09-17-01-07-44\zhixia-app"
AND = r"C:\Users\limingyi\.workbuddy\binaries\android"
jhs = [c for c in sorted(glob.glob(r"C:\Program Files\Java\*")) if os.path.exists(os.path.join(c, "bin", "java.exe"))]
env = dict(os.environ)
env["JAVA_HOME"] = jhs[0]
env["ANDROID_HOME"] = os.path.join(AND, "sdk")
env["GRADLE_USER_HOME"] = os.path.join(AND, "gradle-home")
env["PATH"] = os.path.join(jhs[0], "bin") + ";" + env["PATH"]
p = subprocess.run([os.path.join(AND, "gradle-8.7", "bin", "gradle.bat"), "-p", ROOT,
                    "assembleDebug", "--no-daemon"], capture_output=True,
                   text=True, encoding="utf-8", errors="replace", env=env, timeout=500)
print("RC", p.returncode)
apk = os.path.join(ROOT, "app", "build", "outputs", "apk", "debug", "app-debug.apk")
if os.path.exists(apk):
    dst = r"C:\Users\limingyi\WorkBuddy\2026-09-17-01-07-44\角色.apk"
    shutil.copyfile(apk, dst)
    print("APK", os.path.getsize(dst))
else:
    print("NO APK"); print((p.stdout or "")[-400:])
