# -*- coding: utf-8 -*-
"""所有路径的唯一出处 —— 想挪文件夹，只改这一个文件。

两条规矩：你会手动改的放 config/，程序自己写的放 data/。
新代码一律从这里 import，不要再自己拼 os.path.join。

config/  config.json 主配置、world.json 世界设定、personas/ 人设
plugins/ 插件：每个 .py 是一个功能块（reminder 闹钟、calc 算数…），
         加新功能只往这里丢文件，核心代码不改
data/    memory 记忆库、her_life 经历流、journal 日记、summary 历史摘要、
         chat_history.jsonl 聊天存档、upload 手机发来的图、run 运行时标记
logs/watchdog.log 看门狗日志        archive/ 备份与旧数据
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# --- 四个大类 ---
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
LOGS_DIR = os.path.join(ROOT, "logs")
ARCHIVE_DIR = os.path.join(ROOT, "archive")
# 代码目录，不归上面四类管。插件跟核心代码一起走版本管理，所以放这儿
PLUGINS_DIR = os.path.join(ROOT, "plugins")

# --- config/：你会手动改的 ---
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
WORLD_PATH = os.path.join(CONFIG_DIR, "world.json")
PERSONA_DIR = os.path.join(CONFIG_DIR, "personas")

# --- data/：程序自己写的 ---
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
LIFE_DIR = os.path.join(DATA_DIR, "her_life")
JOURNAL_DIR = os.path.join(DATA_DIR, "journal")
SUMMARY_DIR = os.path.join(DATA_DIR, "summary")      # 历史摘要（聊过什么）
UPLOAD_DIR = os.path.join(DATA_DIR, "upload")
STICKERS_DIR = os.path.join(DATA_DIR, "stickers")    # 她收藏的表情包
PLUGIN_DATA_DIR = os.path.join(DATA_DIR, "plugins")  # 插件自己的持久化
RUN_DIR = os.path.join(DATA_DIR, "run")
CHAT_LOG = os.path.join(DATA_DIR, "chat_history.jsonl")

# --- 渠道隔离 ---
# 她同时在手机 App 和微信上跟同一个人说话，两边要是共用一份历史，
# 在 App 里说的"我在宿舍"会顺着微信那头的上下文往下编。
# 每个渠道一份聊天存档，互不读取。渠道名同时用于记忆库分区（见 memory_store_v2）。
# 生活流和朋友圈不分区 —— 那是同一个人在过同一种日子，分开反而不一致。
CHANNELS = ("app", "wechat")


def chat_log_for(src="app"):
    """按渠道取聊天存档路径。app 沿用老文件名，老数据不用搬。"""
    src = (src or "app").strip().lower()
    if src not in CHANNELS:
        src = "app"
    if src == "app":
        return CHAT_LOG
    return os.path.join(DATA_DIR, "chat_history.%s.jsonl" % src)

# --- data/run/：运行时标记 ---
PET_STAMP = os.path.join(RUN_DIR, "pet_stamp")       # 看门狗上次拉桌宠的时间
CATCHUP_LOCK = os.path.join(RUN_DIR, "catchup.lock")  # 生活补算的跨进程锁

# --- logs/ ---
WATCHDOG_LOG = os.path.join(LOGS_DIR, "watchdog.log")

# 会自动建出来的目录
_DIRS = (CONFIG_DIR, DATA_DIR, LOGS_DIR, ARCHIVE_DIR, PERSONA_DIR,
         MEMORY_DIR, LIFE_DIR, JOURNAL_DIR, SUMMARY_DIR, UPLOAD_DIR,
         STICKERS_DIR, RUN_DIR, PLUGINS_DIR, PLUGIN_DATA_DIR)


def ensure():
    """把该有的目录都建出来。程序启动时调一次就行。"""
    for d in _DIRS:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


# 旧名字兼容：这些常量以前叫别的，留着免得漏改一处就炸
CFG_PATH = CONFIG_PATH
