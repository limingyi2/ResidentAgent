# -*- coding: utf-8 -*-
"""所有路径的唯一出处 —— 想挪文件夹，只改这一个文件。

为什么要有它
-----------
以前每个模块各写各的 `os.path.join(dirname(__file__), "xxx")`，
结果配置和数据全堆在根目录，加个新东西也不知道该往哪放。

现在定死两条规矩（新代码照着来，别再自己拼路径）：

    你会手动改的   ->  config/     （config.json、world.json、personas/）
    程序自己写的   ->  data/       （记忆、经历、日记、聊天存档、图片…）

用法
----
    from paths import CONFIG_PATH, DATA_DIR, LIFE_DIR, ...
    # 或者整个模块：import paths; paths.ensure()

目录结构
-------
    config/config.json          主配置
    config/world.json           她的世界设定（学校/课表/室友）—— 也是你要改的
    config/personas/*.json      人设

    data/memory/                记忆库（关于你的事实）
    data/her_life/              她自己的经历流和状态
    data/journal/               她自己写的日记
    data/summary/               历史摘要（聊过什么的压缩回顾，按天存）
    data/chat_history.jsonl     你俩的聊天存档
    data/upload/                手机发来的图片
    data/run/                   运行时的小标记（心跳、锁）—— 别手动删

    logs/watchdog.log           看门狗日志
    archive/                    备份、旧数据、用不上的东西
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- 四个大类 ----
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
ARCHIVE_DIR = os.path.join(ROOT, "archive")

# ---- config/：你会手动改的 ----
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
WORLD_PATH = os.path.join(CONFIG_DIR, "world.json")
PERSONA_DIR = os.path.join(CONFIG_DIR, "personas")

# ---- data/：程序自己写的 ----
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
LIFE_DIR = os.path.join(DATA_DIR, "her_life")
JOURNAL_DIR = os.path.join(DATA_DIR, "journal")
SUMMARY_DIR = os.path.join(DATA_DIR, "summary")      # 历史摘要（聊过什么）
UPLOAD_DIR = os.path.join(DATA_DIR, "upload")
STICKERS_DIR = os.path.join(DATA_DIR, "stickers")    # 她收藏的表情包
RUN_DIR = os.path.join(DATA_DIR, "run")
CHAT_LOG = os.path.join(DATA_DIR, "chat_history.jsonl")

# ---- data/run/：运行时标记 ----
PET_STAMP = os.path.join(RUN_DIR, "pet_stamp")       # 看门狗上次拉桌宠的时间
CATCHUP_LOCK = os.path.join(RUN_DIR, "catchup.lock")  # 生活补算的跨进程锁

# ---- logs/ ----
WATCHDOG_LOG = os.path.join(LOGS_DIR, "watchdog.log")

# 会自动建出来的目录
_DIRS = (CONFIG_DIR, DATA_DIR, LOGS_DIR, ARCHIVE_DIR, PERSONA_DIR,
         MEMORY_DIR, LIFE_DIR, JOURNAL_DIR, SUMMARY_DIR, UPLOAD_DIR,
         STICKERS_DIR, RUN_DIR)


def ensure():
    """把该有的目录都建出来。程序启动时调一次就行。"""
    for d in _DIRS:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


# 兼容旧名字：以前这些常量叫这些，留着免得漏改一处就炸
CFG_PATH = CONFIG_PATH
