# -*- coding: utf-8 -*-
"""看门狗 —— 桌宠掉线就自己拉起来。

为什么需要它
-----------
她的大脑在云服务器上（`brain/server.py`），但**"人常常不在电脑前"这件事没变**：
开机自启偶尔会失败、桌宠会崩、会被误关 —— 崩了没人管，桌宠就一直不在，
你想找她说话时她连个壳都没有（手机 App 照样能聊，但桌宠那份"在场感"就断了）。

这个脚本由 Windows 计划任务每 5 分钟叫一次，跑完就退，不常驻。

怎么判断桌宠在不在
--------------
只看**单实例互斥量**（`Global\\LinZhixiaPetSingleInstance`）被不被占。
它由内核持有：进程活着就占着、进程一崩立刻释放，而且**刚启动还没建窗口那段也算在**。

  ⚠️ 2026-09-29 起不再探端口：桌宠已经变成纯客户端，本地没有任何 HTTP 服务了
  （原来的 8787 局域网接口已摘掉），再探端口会一直判"她不在"，
  然后每 5 分钟重复拉起一次 —— 一晚上能叠出十几个"已经在运行"的弹窗。

  ⚠️ 别改用 PowerShell 去列进程的命令行（`Get-CimInstance Win32_Process`）：
  实测那条路会卡住，而且卡住的进程握着日志文件的句柄，
  下一次运行就写不进去、直接失败 —— 一个"看门的"把自己看死了。

行为
----
    桌宠不在 → 拉起桌宠（三分钟内只拉一次，免得它还在启动就被重复拉起）
    桌宠在   → 什么都不做

想彻底停掉：config.json 里 `watchdog.keep_pet = false`（允许桌宠关着不复活）。

用法
----
    python watchdog.py            # 检查并按需拉起
    python watchdog.py --dry      # 只看状态，不动手
"""
import os
import io
import sys
import json
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN = os.path.join(os.path.dirname(HERE), "brain")   # 云端大脑（server / config / data）所在目录
if BRAIN not in sys.path:
    sys.path.insert(0, BRAIN)

PET_BAT = os.path.join(HERE, "pet.py")          # 桌宠：本目录
BRAIN_PY = os.path.join(BRAIN, "server.py")   # 云端大脑服务：brain 目录

try:
    import paths
    CFG_PATH = paths.CONFIG_PATH
    PET_STAMP = paths.PET_STAMP        # 上一次拉桌宠的时间
    LOG_PATH = paths.WATCHDOG_LOG      # logs/watchdog.log
    paths.ensure()
except Exception:
    CFG_PATH = os.path.join(BRAIN, "config", "config.json")
    PET_STAMP = os.path.join(BRAIN, "data", "run", "pet_stamp")
    LOG_PATH = os.path.join(BRAIN, "logs", "watchdog.log")

PET_COOLDOWN = 180      # 秒：拉过桌宠之后这段时间内不再重复拉
LOG_MAX = 200 * 1024    # 日志超过这个大小就从头再来


def log(s):
    """既打印也写文件。

    自己写文件而不是让 bat 用 '>>' 重定向 —— 重定向是 cmd 持有句柄，
    一旦有进程卡住不退出，下一次运行就会"文件被占用"直接失败。
    """
    line = time.strftime("[%m-%d %H:%M:%S] ") + s
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX:
            os.remove(LOG_PATH)
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_cfg():
    try:
        with io.open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def pet_singleton_held():
    """True 表示已经有桌宠进程占着单实例互斥量（在跑 或 正在启动）。

    看门狗靠端口判断桌宠在不在；但桌宠刚启动那二三十秒端口还没开，
    端口判断会误以为"不在"而去重复拉起，结果撞上 pet.py 的守卫、
    弹一堆"已经在运行"的 MessageBox。这里用互斥量补一刀：只要互斥量
    被占着就当她在，不重复拉。
    """
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenMutexW(0x00100000, False, "Global\\LinZhixiaPetSingleInstance")
        if h:
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def start_headless(script):
    """无窗口拉起配套进程（桌宠）。

    用 CREATE_NO_WINDOW（0x08000000）彻底不弹控制台。
    """
    exe = sys.executable
    subprocess.Popen([exe, os.path.join(HERE, script)], cwd=HERE,
                     creationflags=0x08000000)
    log("已拉起 " + os.path.basename(script))


def start_bat(path):
    """兼容旧调用名；现在直接无窗口拉起 .py。"""
    start_headless(path)


def pet_cooldown_left():
    try:
        return max(0.0, PET_COOLDOWN - (time.time() - os.path.getmtime(PET_STAMP)))
    except OSError:
        return 0.0


def touch(path):
    try:
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def main():
    dry = "--dry" in sys.argv
    cfg = load_cfg()

    wd = cfg.get("watchdog")
    if not isinstance(wd, dict):
        wd = {}
    want_pet = bool(wd.get("keep_pet", True))

    # 桌宠在不在：只看互斥量（本地接口 2026-09-29 已摘掉，端口没得探了）
    pet_up = pet_singleton_held()

    log("桌宠：%s" % ("在" if pet_up else "不在"))

    if pet_up:
        log("都在，不用管。")
        return 0

    # ---- 桌宠不在：把她拉起来 ----
    if not want_pet:
        log("桌宠不在，但 watchdog.keep_pet = false，不动。")
        return 0
    left = pet_cooldown_left()
    if left > 0:
        log("桌宠不在，但 %.0f 秒前刚拉过，多半还在启动，再等等。"
            % (PET_COOLDOWN - left))
        return 0
    if dry:
        log("--dry：本来该拉起桌宠，这次不动手。")
        return 0
    if not os.path.isfile(PET_BAT):
        log("找不到 " + PET_BAT)
        return 1
    start_bat(PET_BAT)
    touch(PET_STAMP)
    log("桌宠已拉起（要等二三十秒才上线，下一轮会确认）")
    return 0


if __name__ == "__main__":
    # 单实例守卫：同一时间只允许一个看门狗，否则两个会抢着拉桌宠。
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "Global\\LinZhixiaWatchdogSingleInstance")
        if kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            print("[看门狗] 看门狗已在运行，本实例退出。", flush=True)
            sys.exit(0)
    except Exception:
        pass
    sys.exit(main())
