# -*- coding: utf-8 -*-
"""全进程共享的配置与状态（原来糊在 server.py 开头的那一坨）。

这里只放"各模块都要用、且必须是同一个对象"的东西：
- api_config / API_CFG 是**可变共享字典**：路由里换人设/换声音/换模型都是原地
  clear+update，所有模块拿到的都是同一份。谁都不许重新赋值（rebind），一 rebind
  就分成两张皮，"看着换了实际没换"。
- _LOCAL 是大脑进程的唯一位柄；trace 落盘也归这里。
"""
import os
import re
import sys
import json
import threading

# 计划任务启动时 stdout 是 GBK：回复里带 emoji，print 就抛 UnicodeEncodeError，
# 整个流程断在"发送"之前（表现为"生成了回复但对方收不到"）。强制 UTF-8 + replace
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import paths
    CFG_PATH = paths.CONFIG_PATH
    WORLD_PATH = paths.WORLD_PATH        # 她的世界设定（自拍按此刻的课/时段挑地点）
except Exception:
    CFG_PATH = os.path.join(HERE, "config", "config.json")
    WORLD_PATH = os.path.join(HERE, "config", "world.json")


# --- 配置 ---

def load_config():
    """读 config.json。读不到就用空 dict —— 后面给的是人话报错，不是栈。"""
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[大脑] 读不了 config.json：{e}", flush=True)
        return {}


api_config = load_config()

# 访问码优先读 brain_token，旧配置的 mobile.token 继续认。这个兜底不能删：
# 读不到会变成空串，_check() 直接放行，等于撤掉鉴权
MOB_CFG = api_config.get("mobile") or {}
if not isinstance(MOB_CFG, dict):
    MOB_CFG = {}
BRAIN_TOKEN = str(api_config.get("brain_token") or MOB_CFG.get("token") or "")

# 白名单式浅拷贝：这里漏掉哪个键，对应模块就读到空配置、改了也不生效
# （vision 与 voice 各栽过一次，uapi 也一直漏着 —— 所有 UAPI 调用都没带上 token）。
# 往 config 加新功能段时记得同步这里
API_CFG = {k: api_config[k] for k in
           ("api_base", "api_key", "model", "vision", "voice", "uapi")
           if k in api_config}


def config_path():
    """config.json 的绝对路径（路由里写人设/声音用，别处别再拼一遍）。"""
    try:
        from paths import CONFIG_DIR
        return os.path.join(str(CONFIG_DIR), "config.json")
    except Exception:
        return os.path.join(HERE, "config", "config.json")


# --- 大脑位柄 ---

_LOCAL = {"brain": None, "lock": threading.Lock()}


# --- trace：每轮对话怎么产生的 ---

def _trace_path():
    try:
        from paths import DATA_DIR
        return os.path.join(str(DATA_DIR), "trace.jsonl")
    except Exception:
        return r"C:\linzhixia\data\trace.jsonl"


def _trace_write(rec):
    """每轮对话落一行 trace。

    记的是"这轮是怎么产生出来的"：命中了哪几条记忆（含相似度）、注入了哪些块各多少
    字、约定账本有没有带上、去重与静默闸门拦没拦、用了哪个模型、延迟、token、
    是不是走了兜底。**只记结构化的中间数据**，不记模型内心活动。
    写文件失败一律静默：观测手段不能反过来把聊天搞挂。
    """
    try:
        p = _trace_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _trace_tail(n=20):
    p = _trace_path()
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        return []
    return out[-int(n or 20):]


# 供路由统计用（_stats_today 在 server.py，因为还要搭 brain/voice 的状态）
GEN_TAG_RE = re.compile(r"^\s*\[gen[:：]\s*([^\]]+?)\]\s*$", re.M)


def data_file(name, win_fallback=""):
    """data 目录下某文件的路径（paths 在就听 paths 的，不在给 Windows 兜底）。"""
    try:
        from paths import DATA_DIR
        return os.path.join(str(DATA_DIR), name)
    except Exception:
        return win_fallback or os.path.join(HERE, "data", name)
