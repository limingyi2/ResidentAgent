# -*- coding: utf-8 -*-
"""主动搭话：她什么时候可以主动找他、多久一次、说什么不算骚扰。

interval、静默时段、每日上限都必须从 config 的 proactive 段读 ——
写死的话用户就算设了静默，照样凌晨被连发消息。
他从哪连进来的（IP）也归这里管：天气城市跟着这个 IP 定位。
"""
import os
import re
import json
import time

from runtime import data_file

PROACTIVE_RULES = """先看清楚现在几点、再看看要不要主动找他聊两句。

判断依据：
- 真遇到想分享的事、突然想起他、或者单纯想他了，才主动
- 真人不会每小时都主动找人，大多数时候什么都不说
- 先看一眼你们最近的对话：同一件事你已经问过、催过或说过的，绝不再提第二遍。
  哪怕他一直没回也先放着——真人不会追着人重复问同一句，想聊就换个别的话题，
  或者干脆这轮"无"
- 看一眼对话里每条前面的方括号时间：要是那句"约定的那个周末"是今早甚至更早
  说的，那都是过去说过的话，不是此刻正在谈的事 —— 别再翻出来催，也别把
  已经过去的日子当成还没到（今天几号，看最上面那句当前时间）。
- 你们之间已经定好的安排（比如假期谁去找谁、票和酒店谁负责），就按定好的说；
  记忆里写的是"已定"就别再当"没准话"来催，拿不准方向就干脆不提这件事，
  绝不能自己换个方向重问、更不能编没人说过的细节（比如"谁说票价要涨"）

输出格式（严格二选一）：
不说 -> 只输出一个字：无
想说 -> 第一行只写一个字：说
        第二行开始就是你发给他的话（一两句，口语，别写作文）

除这两种内容外，不要输出任何别的话。"""


def _proactive_conf():
    """读 config.json 里的 proactive 段。读不到就返回空 dict（走代码默认值）。"""
    from runtime import config_path
    try:
        cfg = json.load(open(config_path(), encoding="utf-8"))
    except Exception:
        return {}
    pa = cfg.get("proactive")
    return pa if isinstance(pa, dict) else {}


def _parse_hhmm(s):
    """'23:00' / '7:30' / '24:00' → 当天第几分钟；不合法返回 None。

    24:00 合法（=1440，用来收一天的尾巴），24:01 这类不合法。解析不出来等于没设
    静默，会照发不误。
    """
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (h == 24 and mi != 0):
        return None
    return h * 60 + mi


def _in_quiet(start, end, cur=None):
    """现在是否处于静默时段（这期间不主动搭话）。支持跨零点，如 23:00~07:00。

    cur 传"当天第几分钟"可脱离真实时间测试。起止任一解析不出来、或两者相等，一律
    当"没设静默"—— 与其猜错把她整天闷住，不如照常说话，用户改配置即可。
    """
    a, c = _parse_hhmm(start), _parse_hhmm(end)
    if a is None or c is None or a == c:
        return False
    if cur is None:
        lt = time.localtime()
        cur = lt.tm_hour * 60 + lt.tm_min
    if a < c:
        return a <= cur < c          # 同日区间，如 13:00~14:00
    return cur >= a or cur < c       # 跨零点区间，如 23:00~07:00


# --- 他从哪连进来的 ---

_LAST_IP = {"ip": ""}


def note_client_ip(ip):
    """记下他最近的公网 IP（内网/回环不算 —— 桌宠走 SSH 隧道进来是 127.0.0.1）。"""
    s = str(ip or "")
    if not s or s.startswith(("127.", "10.", "192.168.", "172.16.", "172.17.",
                              "172.18.", "172.19.", "172.2", "172.30.", "172.31.",
                              "169.254.", "::1")):
        return
    _LAST_IP["ip"] = s


def _his_ip():
    return _LAST_IP["ip"]


# --- 自适应频率的记账 ---

def _proactive_quota_file():
    return data_file("proactive_quota.json")


def _proactive_stats_file():
    return data_file("proactive_stats.json",
                     r"C:\linzhixia\data\proactive_stats.json")


def _proactive_stats_load():
    try:
        d = json.load(open(_proactive_stats_file(), encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("sent", [])
            d.setdefault("last_user_ts", 0)
            return d
    except Exception:
        pass
    return {"sent": [], "last_user_ts": 0}


def _proactive_stats_save(d):
    try:
        d["sent"] = (d.get("sent") or [])[-40:]
        p = _proactive_stats_file()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _proactive_note_sent():
    """她主动发了一条 —— 记下来，等他的反应来评判这次该不该发。"""
    d = _proactive_stats_load()
    d["sent"].append({"ts": int(time.time()), "replied": 0})
    _proactive_stats_save(d)


def _proactive_note_user():
    """他说话了 —— 把最近 3 小时内还没被回应的那条主动消息标成"他回了"。

    这是自适应频率唯一的反馈信号：主动发了之后他理不理。
    """
    now = int(time.time())
    d = _proactive_stats_load()
    for s in reversed(d.get("sent") or []):
        if s.get("replied"):
            continue
        if now - int(s.get("ts") or 0) <= 3 * 3600:
            s["replied"] = 1
            s["gap"] = now - int(s.get("ts") or 0)
        break                      # 只认最近那一条
    d["last_user_ts"] = now
    _proactive_stats_save(d)


def _proactive_interval_multiplier():
    """按最近主动消息的"被回应率"给间隔乘一个系数。

    真人的分寸感来自反馈：最近 10 次里 ≥0.5 → ×1.0，0.25~0.5 → ×1.5，
    <0.25 → ×2.0。另外他 15 分钟内说过话就这轮推迟 —— 他人在，不需要"找"他。
    """
    d = _proactive_stats_load()
    sent = (d.get("sent") or [])[-10:]
    replied = sum(1 for s in sent if s.get("replied"))
    rate = (replied / float(len(sent))) if sent else None
    if rate is None:
        return 1.0
    if rate >= 0.5:
        return 1.0
    if rate >= 0.25:
        return 1.5
    return 2.0


def _proactive_used_today():
    """今天已经主动搭话几次（跨天自动归零）。"""
    try:
        d = json.load(open(_proactive_quota_file(), encoding="utf-8"))
    except Exception:
        return 0
    if str(d.get("date") or "") != time.strftime("%Y-%m-%d"):
        return 0
    try:
        return int(d.get("n") or 0)
    except Exception:
        return 0


def _proactive_count_up():
    """主动搭话计数 +1 并落盘（重启不丢）。写不进去也不能影响聊天。"""
    try:
        p = _proactive_quota_file()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"date": time.strftime("%Y-%m-%d"),
                       "n": _proactive_used_today() + 1}, f)
    except Exception:
        pass


# --- 去重：她不能把刚说过的话再说一遍 ---

def _recent_her_texts(n=12):
    """她最近说过的 n 句话（读聊天存档尾部）。

    主动搭话去重要用：她每轮主动说话时，上下文里就有自己刚说过的话，模型会原样
    （或截一段）再吐一次 —— 用户看到的就是"她把同一句话发两遍"。
    """
    from runtime import data_file as _df
    p = _df("chat_history.jsonl", r"C:\linzhixia\data\chat_history.jsonl")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("role") == "assistant":
                    out.append(d.get("text") or "")
    except OSError:
        return []
    return out[-int(n or 12):]


def _norm_text(s):
    """比对用的归一化：去空白、去时间戳前缀、全角转半角太麻烦只做基本清洗。"""
    s = str(s or "").strip()
    s = re.sub(r"^\[\d{1,4}-\d{1,2}-\d{1,2}[ T]?\d{0,2}:?\d{0,2}\]?\s*", "", s)
    return re.sub(r"\s+", "", s)


def _is_repeat_of_recent(text, n=12):
    """这句话是不是她最近已经说过的（含"截一段再说"的情况）。"""
    cur = _norm_text(text)
    if not cur:
        return False
    for old in _recent_her_texts(n):
        o = _norm_text(old)
        if not o:
            continue
        if cur == o:
            return True
        # 长的一条里截出短的一条（12:44 那条就是 12:37 那句的第一行）
        if (len(cur) >= 8 and cur in o) or (len(o) >= 8 and o in cur):
            return True
    return False


def _cloud_append_assistant(text):
    """往聊天存档追加一条她说的话（主动搭话用）。"""
    from runtime import data_file as _df
    p = _df("chat_history.jsonl", r"C:\linzhixia\data\chat_history.jsonl")
    row = {"t": time.strftime("%Y-%m-%d %H:%M"), "role": "assistant",
           "text": text}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
