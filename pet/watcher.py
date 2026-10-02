# -*- coding: utf-8 -*-
"""活动感知模块 —— 让角色知道你在干嘛

只做两件很轻的事，**完全不截屏、不碰任何视觉模型**：
1. 读前台窗口的进程名和标题（ctypes 直接调 Windows API，开销几乎为零）
2. 从标题 / 进程名猜你在干嘛（刷 B 站 / 写代码 / 打游戏…）

猜到你在做不同的事时回调 on_activity —— 这就是"主动搭话"的入口；
前台切成"游戏 / 铺满全屏"时回调 on_game，立绘据此自动让开、不挡画面。

（以前这里还能截屏 + 用视觉模型看图，已整体移除：截图上传有隐私成本、本地模型占
显存，而且实测会把屏幕上的文字抄下来当话题，说些莫名其妙的话。）
"""

import time, ctypes, threading, datetime
import ctypes.wintypes as wt

import psutil


# --- 前台窗口 ---

_user32 = ctypes.windll.user32


def get_foreground():
    """拿当前前台窗口的进程名和标题。

    这一步非常轻（就是读两个系统 API），可以每秒跑很多次，对游戏帧率的影响可以忽略。
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return {"exe": "", "title": "", "pid": 0}

    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value

    exe = ""
    try:
        exe = psutil.Process(pid.value).name()
    except Exception:
        pass

    # 判断是不是全屏（窗口铺满整个屏幕）
    rect = wt.RECT()
    fullscreen = False
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        sw = _user32.GetSystemMetrics(0)
        sh = _user32.GetSystemMetrics(1)
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        fullscreen = (w >= sw * 0.98 and h >= sh * 0.98)

    return {"exe": exe, "title": title, "pid": pid.value, "fullscreen": fullscreen}


# --- 游戏判断 ---

# 常见游戏进程名（小写）。命中就认为在打游戏。
GAME_EXE = {
    # 腾讯
    "leagueclient.exe", "league of legends.exe", "crossfire.exe", "dnf.exe",
    "lolclient.exe", "game_loader.exe", "pubgmhd.exe", "和平精英",
    # Steam / 通用
    "steam.exe",  # 注意：Steam 客户端本身不算游戏，见下面白名单
    # 米哈游
    "genshinimpact.exe", "yuanshen.exe", "star rail.exe", "starrail.exe",
    "honkaiimpact3.exe", "bh3.exe", "zenlesszonezero.exe", "nap.exe",
    # 其他常见
    "valorant.exe", "valorant-win64-shipping.exe", "csgo.exe", "cs2.exe",
    "dota2.exe", "overwatch.exe", "wow.exe", "ffxiv.exe", "ffxiv_dx11.exe",
    "minecraft.exe", "javaw.exe",  # javaw 多为 MC
    "eldenring.exe", "sekiro.exe", "darksoulsiii.exe",
    "cod.exe", "codmw.exe", "battlefield.exe", "bf2042.exe",
    "apexlegends.exe", "r5apex.exe", "fortnite.exe", "fortniteclient-win64-shipping.exe",
    "gta5.exe", "rdr2.exe", "cyberpunk2077.exe", "witcher3.exe",
    "nba2k.exe", "fifa.exe", "fc24.exe", "fc25.exe",
    "wutheringwaves.exe", "wwmi.exe",  # 鸣潮
    "reverse1999.exe", "azurlane.exe", "arknights.exe", "arknights.exe",
    "toweroffantasy.exe", "tof.exe",
    "diablo iv.exe", "diabloiv.exe", "pathofexile.exe", "poe.exe",
    "tft.exe", "hearthstone.exe", "hs.exe", "magicarena.exe",
    "mhw.exe", "monsterhunterworld.exe", "mhrise.exe",
    "naraka.exe", "narakabladepoint.exe",  # 永劫无间
    "deltaforce.exe", "df.exe",  # 三角洲行动
    "squad.exe", "hellletloose.exe", "hll.exe",
    "rocketleague.exe", "warframe.exe", "destiny2.exe", "terraria.exe",
    "stardewvalley.exe", "stardew valley.exe", "hollowknight.exe",
    "ori.exe", "celeste.exe", "cuphead.exe",
    "forzahorizon5.exe", "forzahorizon4.exe", "needforspeed.exe",
    "sims4.exe", "thesims4.exe", "cities.exe", "citiesskylines.exe",
    "factorio.exe", "rimworld.exe", "dyson sphere program.exe",
    "subnautica.exe", "valheim.exe", "palworld.exe", "幻兽帕鲁",
    "blackmythwukong.exe", "b1.exe", "wukong.exe",  # 黑神话悟空
    "sdgundam.exe", "gundam.exe",
    "lastepoch.exe", "grimdawn.exe", "torchlight.exe",
    "lostark.exe", "lost ark.exe", "maplestory.exe",
    "runescape.exe", "osrs.exe", "tibia.exe",
    "totalwar.exe", "civilization.exe", "civ6.exe",
    "xplane.exe", "fs2020.exe", "flightsimulator.exe",
    "yuzu.exe", "rpcs3.exe", "cemu.exe", "dolphin.exe", "pcsx2.exe",  # 模拟器
    "mumu.exe", "ldplayer.exe", "bluestacks.exe", "nox.exe",  # 安卓模拟器
}

# 这些虽然在上面集合里，但只是启动器/客户端，不算真在玩
NOT_REALLY_PLAYING = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
    "wegame.exe", "uplay.exe", "origin.exe", "battle.net.exe",
    "riotclientservices.exe", "riotclient.exe", "discord.exe",
}

# 从窗口标题判断在玩/在看什么（比进程名更准）
TITLE_HINTS = {
    "b站": "在刷 B 站", "哔哩哔哩": "在刷 B 站", "bilibili": "在刷 B 站",
    "抖音": "在刷抖音", "douyin": "在刷抖音",
    "youtube": "在看 YouTube", "微博": "在刷微博",
    "知乎": "在刷知乎", "小红书": "在刷小红书",
    "贴吧": "在逛贴吧", "twitter": "在看推特", "x.com": "在看推特",
    "netflix": "在看 Netflix", "爱奇艺": "在看爱奇艺",
    "腾讯视频": "在看腾讯视频", "优酷": "在看优酷", "芒果": "在看芒果TV",
    "斗鱼": "在看斗鱼直播", "虎牙": "在看虎牙直播", "twitch": "在看 Twitch",
}


def looks_like_game(fg):
    """判断当前是不是在打游戏（只看进程名，零开销）。"""
    exe = (fg.get("exe") or "").lower()

    if not exe:
        return False
    if exe in NOT_REALLY_PLAYING:
        return False
    return exe in GAME_EXE


def guess_from_title(fg):
    """不看画面，只从窗口标题猜他在干嘛。零开销（标题本身会上云，见 :333）。"""
    title = (fg.get("title") or "").lower()
    exe = (fg.get("exe") or "").lower()

    if exe in ("chrome.exe", "msedge.exe", "firefox.exe", "360se.exe",
               "360chrome.exe", "qqbrowser.exe", "sogouexplorer.exe",
               "opera.exe", "brave.exe", "vivaldi.exe"):
        for kw, desc in TITLE_HINTS.items():
            if kw in title:
                return desc
        return "在逛网页"

    for kw, desc in TITLE_HINTS.items():
        if kw in title:
            return desc

    if exe in ("code.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
               "webstorm64.exe", "notepad++.exe", "sublime_text.exe",
               "cursor.exe", "trae.exe"):
        return "在写代码"
    if exe in ("winword.exe", "wps.exe", "et.exe"):
        return "在写文档"
    if exe in ("excel.exe",):
        return "在做表格"
    if exe in ("qq.exe", "wechat.exe", "weixin.exe", "dingtalk.exe",
               "feishu.exe", "lark.exe"):
        return "在聊天"
    if exe in ("potplayer.exe", "vlc.exe", "mpc-hc.exe", "wmplayer.exe"):
        return "在看视频"
    if exe in ("cloudmusic.exe", "qqmusic.exe", "kugou.exe", "spotify.exe"):
        return "在听歌"
    return ""


# --- 主循环 ---

DEFAULT_CFG = {
    "enabled": True,        # 总开关
    "probe_sec": 5,         # 前台检测间隔（秒）—— 这个很轻，5 秒一次没问题
    "cooldown": 600,        # 游戏开局那句话的冷却（秒），防止太吵
    "game_pause": True,     # 检测到游戏时走"打游戏"分支
    "title_proactive_interval": 180,  # 两次主动搭话的最小间隔（秒）
}

# 主动搭话的策略（设置窗口里可改）
DEFAULT_PROACTIVE = {
    "enabled": True,             # 总开关
    "max_per_day": 8,            # 每天最多主动说几句（到量就不再开口）
    "quiet_start": "23:00",      # 免打扰开始
    "quiet_end": "08:00",        # 免打扰结束
    "on_activity_change": True,  # 活动切换时主动说一句
    "on_game_start": True,       # 你开始打游戏时说一句
    "on_startup": True,          # 桌宠启动后主动打个招呼
}


class ActivityWatcher:
    """监控你在干嘛（只看窗口标题 / 进程名，不截屏）。

    刻意不用 QThread —— 用普通线程 + 回调，单独测试方便，pet.py 再用 Qt 信号接到界面上。
    """

    def __init__(self, api_config, cfg=None, on_activity=None, on_status=None,
                 on_game=None):
        self.api = api_config or {}
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(cfg or {})
        self.on_activity = on_activity     # 抓到"值得说"的内容时回调
        self.on_status = on_status         # 每次状态变化时回调（轻微，不打扰）
        self.on_game = on_game             # 前台"打游戏 / 铺满全屏"跳变时回调

        self._stop = threading.Event()
        self._thread = None
        self._last_speak = 0.0
        self._last_fg = {}
        self._last_game = False
        self._last_block = False      # 上一次"该让开"的状态（游戏或全屏）
        self.state = "idle"

        # 主动搭话策略
        self.pro = dict(DEFAULT_PROACTIVE)
        if isinstance(self.cfg.get("proactive"), dict):
            self.pro.update(self.cfg["proactive"])
        self._count_date = None
        self._count_today = 0

    # --- 主动搭话的闸门 ---
    def set_proactive(self, pro):
        """设置窗口保存后热更新。"""
        if isinstance(pro, dict):
            self.pro.update(pro)

    def _in_quiet(self):
        qs = (self.pro.get("quiet_start") or "").strip()
        qe = (self.pro.get("quiet_end") or "").strip()
        if not qs or not qe:
            return False
        try:
            h1, m1 = map(int, qs.split(":"))
            h2, m2 = map(int, qe.split(":"))
        except Exception:
            return False
        a, b = h1 * 60 + m1, h2 * 60 + m2
        if a == b:
            return False
        t = datetime.datetime.now()
        cur = t.hour * 60 + t.minute
        if a < b:                       # 同一天内，如 12:00–14:00
            return a <= cur < b
        return cur >= a or cur < b      # 跨午夜，如 23:00–08:00

    def _quota_ok(self):
        today = datetime.date.today().isoformat()
        if self._count_date != today:
            return True
        return self._count_today < int(self.pro.get("max_per_day", 8))

    def _may_speak(self):
        if not self.pro.get("enabled", True):
            return False
        if self._in_quiet():
            return False
        return self._quota_ok()

    def _bump_count(self):
        today = datetime.date.today().isoformat()
        if self._count_date != today:
            self._count_date = today
            self._count_today = 0
        self._count_today += 1

    # --- 控制 ---
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def set_enabled(self, v):
        self.cfg["enabled"] = bool(v)

    # --- 主循环 ---
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[watcher] tick 出错: {e}", flush=True)
            self._stop.wait(self.cfg["probe_sec"])

    def _tick(self):
        if not self.cfg["enabled"]:
            return

        fg = get_foreground()
        if not fg.get("exe"):
            return

        now = time.time()

        # --- 打游戏：只用进程名判断 ---
        playing = looks_like_game(fg)

        # 「在打游戏」或「前台窗口铺满整个屏幕」都该让立绘躲开。
        # 全屏那条是兜底：名字没收录的新游戏、F11 全屏的视频网站同样会盖住她。
        blocking = playing or bool(fg.get("fullscreen"))
        if blocking != self._last_block:
            self._last_block = blocking
            self._emit_game(playing, bool(fg.get("fullscreen")), fg)

        if playing and self.cfg["game_pause"]:
            if not self._last_game:
                self._last_game = True
                self._emit_status("在打游戏", fg, source="process")
                if (self.pro.get("on_game_start", True)
                        and now - self._last_speak >= self.cfg["cooldown"]
                        and self._may_speak()):
                    self._emit_activity({
                        "what": "打游戏",
                        "detail": f"打开了 {fg.get('exe','')}".replace(".exe", ""),
                        "mood": "专注",
                        "hook": "开局",
                    }, fg, source="process")
            return

        if self._last_game:
            self._last_game = False   # 游戏结束了

        # --- 只看窗口标题猜他在干嘛（零开销；隐私见下） ---
        # 别把这句写成"零隐私成本"：不截屏是真的（截屏那条路已整体移除，见文件头），
        # 但前台窗口的标题原文会随 scene 一起发到云上，标题里常有文档名、
        # 聊天对象、搜索词。比截屏轻得多，但不是零。
        guess = guess_from_title(fg)
        prev = self._last_fg.get("guess")
        if guess and guess != prev:
            self._last_fg["guess"] = guess
            self._emit_status(guess, fg, source="title")
            # 活动从一件事切到另一件事时让她主动说一句 —— 这是"主动搭话"的入口。
            # 用独立间隔挡着，避免切窗口太频繁时刷屏；启动后的第一次检测不算（prev 为 None），
            # 不冷不丁开口
            if (self.pro.get("on_activity_change", True)
                    and prev is not None
                    and now - self._last_speak
                    >= self.cfg.get("title_proactive_interval", 180)
                    and self._may_speak()):
                self._emit_activity({
                    "what": guess,
                    "detail": "",
                    "mood": "",
                    "hook": "",
                }, fg, source="title")

    # --- 回调 ---
    def _emit_game(self, playing, fullscreen, fg):
        """前台从"能看见桌面"切到"游戏/全屏"（或反过来）时通知界面。"""
        if not self.on_game:
            return
        try:
            self.on_game({"playing": bool(playing),
                          "fullscreen": bool(fullscreen), "fg": fg})
        except Exception as e:
            print(f"[watcher] on_game 回调出错: {e}", flush=True)

    def _emit_status(self, what, fg, source=""):
        if self.on_status and what:
            self.state = what
            self.on_status({"what": what, "fg": fg, "source": source})

    def _emit_activity(self, d, fg, source=""):
        self._last_speak = time.time()
        self._bump_count()          # 记一次"今天主动说了几句"
        if self.on_activity:
            self.on_activity({"what": d.get("what", ""),
                              "detail": d.get("detail", ""),
                              "mood": d.get("mood", ""),
                              "hook": d.get("hook", ""),
                              "fg": fg, "source": source})
