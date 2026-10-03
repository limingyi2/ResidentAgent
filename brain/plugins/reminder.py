# -*- coding: utf-8 -*-
"""提醒 / 闹钟：他说一句"20分钟后叫我起床"就真的到点主动喊他。

**这个插件是插件系统的活样板**，想加新功能就照着它抄 —— 它把能用的钩子
都用上了：TOOLS（模型自己决定调）、TOOL_HELP（教模型怎么用）、
on_turn_in（他提到时间时把已有提醒塞进上下文）、start（自己的后台线程）。
on_reply 和 prompt 这个用不上，就不写。

数据落在 data/plugins/reminder/，一条一个 json。删文件就等于清空。
"""
import os
import re
import time
import json
import threading

NAME = "reminder"
DESC = "闹钟/提醒：说\"20分钟后叫我\"就到点主动喊他"
FEATURE = "reminder"          # App 设置页自动多一个开关

# 到点最长等 10 秒一轮；每轮只问"到点的没有"，不重扫全部
_TICK = 10
_FILE = "reminders.json"
# 最多留这么多条，超了丢最老的 —— 免得他一年前说的"记得提醒我"还挂着
_MAX = 50

# 中文数字 → 阿拉伯数字。只处理到"九十"，够日常用了
_CN = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
       "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_int(s):
    """'二十三' -> 23。'十'->10，'二十'->20。只支持十位以内。"""
    s = str(s or "").strip()
    if not s:
        return None
    if all(c in _CN for c in s) or (s.startswith("十") and len(s) == 1):
        if s == "十":
            return 10
        if "十" not in s:
            return _CN[s]
        a, _, b = s.partition("十")
        tens = _CN[a] if a else 1
        ones = _CN[b] if b else 0
        return tens * 10 + ones
    return None


def _f64(s):
    """'5' / '5.5' / '半' -> 分钟数。解析不了返回 None。"""
    s = str(s or "").strip()
    if not s:
        return None
    if s in ("半", "半个"):
        return 0.5
    if s in ("一刻",):
        return 0.25
    try:
        return float(s)
    except ValueError:
        pass
    n = _cn_int(s)
    return float(n) if n is not None else None


def _mins(num, unit):
    """把「数量 + 单位」换成分钟。

    单位必须参与换算 —— 「三天」和「三分钟」的数字是一样的，只看数字的话
    三天会被当成 3 分钟，提醒当场就废了（这个错真犯过）。
    """
    n = _f64(num)
    if n is None:
        return None
    u = str(unit or "")
    if u in ("分钟", "分"):
        return n
    if u in ("小时", "钟头", "个小时"):
        return n * 60
    if u == "天":
        return n * 60 * 24
    return None


_DUR = r"(\d+(?:\.\d+)?|[零一两二三四五六七八九十]+|半|半个|一刻)"
_UNIT = r"(分钟|分|小时|钟头|个小时|天)"
# 「20分钟后」「十分钟后」「半个钟头后」「三天后」
_REL_RE = re.compile(_DUR + r"\s*" + _UNIT + r"\s*(?:后|以后|之内)")
_AT_RE = re.compile(r"(今天|明天|后天|晚上|早上|上午|下午|中午)?\s*"
                    r"(\d{1,2})[:：](\d{1,2})")
# 直接报钟点：「八点」「8点半」
_CLOCK_RE = re.compile(r"(\d{1,2}|[零一两二三四五六七八九十]+)\s*点"
                       r"(半|\d{1,2}|整)?")


def _dir():
    try:
        from paths import PLUGIN_DATA_DIR
        d = os.path.join(str(PLUGIN_DATA_DIR), NAME)
    except Exception:
        d = r"C:\linzhixia\data\plugins\reminder"
    os.makedirs(d, exist_ok=True)
    return d


def _load():
    try:
        with open(os.path.join(_dir(), _FILE), encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save(items):
    items = items[-_MAX:]
    tmp = os.path.join(_dir(), _FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(_dir(), _FILE))
    return items


def _clean(t):
    """把一句话里跟时间无关的部分去掉，留个短标题当提醒正文。"""
    t = re.sub(_REL_RE, "", str(t or ""))
    t = re.sub(_AT_RE, "", t)
    t = re.sub(_CLOCK_RE, "", t)
    t = re.sub(r"(?:提醒我|叫我|喊我|别忘了|记得|麻烦你|到点|之后|然后|的话|我)",
               " ", t)
    t = re.sub(r"[的了]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ，。、,.!！?？:：;；-—")
    return t[:40]


def _when(arg):
    """从他说的话里算出触发时刻（绝对时间戳）。算不出返回 None。"""
    a = str(arg or "")
    m = _REL_RE.search(a)
    if m:
        mins = _mins(m.group(1), m.group(2))
        if mins:
            secs = min(mins * 60, 60 * 60 * 24 * 30)   # 最多一个月，再远不像"提醒"
            return time.time() + secs
    now = time.localtime()
    m = _AT_RE.search(a)
    if m:
        day, hh, mm = m.group(1) or "", int(m.group(2)), int(m.group(3))
        if not 0 <= hh <= 23 or not 0 <= mm <= 59:
            return None
        off = {"今天": 0, "明天": 1, "后天": 2}.get(day)
        if off is None:
            off = 2 if hh < 5 else 0        # 没写哪天：凌晨的算明天，其余算今天
        base = time.time() - time.timezone
        t = time.localtime(base + off * 86400)
        ts = _mk(t.tm_year, t.tm_mon, t.tm_mday, hh, mm)
        return ts if ts > time.time() else ts + 86400
    m = _CLOCK_RE.search(a)
    if m:
        h = _cn_int(m.group(1))
        if h is None:
            h = int(m.group(1)) if m.group(1).isdigit() else None
        if h is None or not 0 <= h <= 23:
            return None
        mm = 0
        if m.group(2) == "半":
            mm = 30
        elif m.group(2) and m.group(2).isdigit():
            mm = int(m.group(2))
        if not 0 <= mm <= 59:
            return None
        t = time.localtime()
        ts = _mk(t.tm_year, t.tm_mon, t.tm_mday, h, mm)
        return ts if ts > time.time() else ts + 86400
    return None


def _mk(y, mo, d, h, mi):
    import calendar
    return calendar.timegm((y, mo, d, h, mi, 0, 0, 0, 0)) - time.timezone


# --- 工具：模型自己决定调不调 ---

def _t_set(arg):
    """设一个提醒。参数：他说的原话（模型会把"20分钟后叫我起床"整句传进来）。"""
    ts = _when(arg)
    if not ts:
        return "设不了：没看出是几点。用「20分钟后」「明天早上八点」「八点半」这种说法"
    items = _load()
    body = _clean(arg) or "你要的事"
    items.append({"at": round(ts, 1), "body": body,
                  "src": "tool", "t": time.strftime("%Y-%m-%d %H:%M")})
    _save(items)
    return "设好了：%s 到点提醒他「%s」" % (
        time.strftime("%m月%d日 %H:%M", time.localtime(ts)), body)


def _t_list(arg):
    """看她手上还有哪些提醒。"""
    items = [r for r in _load() if float(r.get("at") or 0) > time.time() - 60]
    if not items:
        return "她手上没有还没到点的提醒"
    return "她记着这些：" + "；".join(
        "%s %s" % (time.strftime("%m-%d %H:%M", time.localtime(float(r["at"]))),
                   r.get("body") or "") for r in items[-6:])


def _t_cancel(arg):
    """取消提醒。参数里含关键词就取消匹配的，说了"全部/都"就清空。

    "全部"要**先**判掉再剥关键词 —— 剥完就没了，结果是"没给关键词"，
    明明她手上挂着三条却一条也删不掉（这个错真犯过）。
    """
    a = str(arg or "")
    items = _load()
    if not items:
        return "她手上本来就没有提醒"
    if re.search(r"全部|都|所有|清空|清掉", a):
        n = len(items)
        _save([])
        return "取消了全部 %d 条提醒" % n
    kw = re.sub(r"(?:全部|都|所有|帮我|取消|删除|删掉|清空|提醒|一下|吧|那个|这个|那条)",
                "", a).strip()
    # 双向包含：他说的词里包着标题（"吃饭" ⊂ "把吃饭那个取消"），或者反过来。
    # 只做单向 kw in body 的话，"把吃饭那个取消"剥完剩"把吃饭"就匹配不上
    keep = [r for r in items
            if kw and (kw in str(r.get("body") or "")
                       or str(r.get("body") or "") in kw)]
    if not keep:
        return "没有能取消的（%s）" % (kw or "没给关键词")
    _save([r for r in items if r not in keep])
    return "取消了：%s" % "；".join(r.get("body") or "" for r in keep[:5])


TOOLS = {"reminder_set": _t_set, "reminder_list": _t_list,
         "reminder_cancel": _t_cancel}


def _route_list(body, query):
    """GET/POST /api/plugin/reminder —— 列出还没到点的提醒（给 App 调试/查看用）。"""
    items = [r for r in _load() if float(r.get("at") or 0) > time.time()]
    return {"ok": True, "items": [
        {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r["at"]))),
         "body": r.get("body") or ""} for r in items]}


ROUTES = [("GET", "/api/plugin/reminder", _route_list),
          ("POST", "/api/plugin/reminder/list", _route_list)]

# 教模型用的。写"有哪些、各是什么"，**不写"什么场景必须用"** ——
# 后者是那种加一个场景就得补一条的规则，永远补不完。
TOOL_HELP = (
    "- 提醒（闹钟）：[tool:reminder_set:他说的原话] 设一个到点主动提醒他的事"
    "（支持「20分钟后」「明天早上八点」「八点半」「三天后」）；"
    "[tool:reminder_list] 看她手上还有哪些提醒；"
    "[tool:reminder_cancel:关键词] 取消（说“全部取消”就传“全部”）。\n"
    "  设了提醒她真的会到点主动喊他，所以只要他说了任何带时间、像是"
    "「待会儿/回头/到时候/别忘了」的事，就设上 —— 他自己不记这个。\n"
    "  提醒已经设过、到了、或者他说的不是时间（比如“明天见”这种约见面），"
    "就别重复设。要查他之前提过什么提醒用 reminder_list，别凭印象说。\n"
)


# --- 上下文钩子 ---

def on_turn_in(ctx):
    """他发来消息时，把还没到点的提醒告诉他。

    不这么做的后果：他在 App 上说"20分钟后叫我"，然后隔一小时又发消息，
    她完全不记得自己该在二十分钟后喊他 —— 提醒就哑了。
    """
    items = [r for r in _load() if float(r.get("at") or 0) > time.time()]
    if not items:
        return ""
    return ("\n（系统提示：她手上还记着这些到点要提醒他的事，"
            "相关的就提一嘴，别主动念清单）\n- " + "；".join(
                "%s 提醒他「%s」" % (
                    time.strftime("%H:%M", time.localtime(float(r["at"]))),
                    r.get("body") or "") for r in items[-5:]))


# --- 后台线程 ---

def _loop():
    while True:
        try:
            now = time.time()
            items = _load()
            due = [r for r in items if float(r.get("at") or 0) <= now]
            if due:
                _save([r for r in items if r not in due])
                for r in due[:5]:
                    body = str(r.get("body") or "那件事")
                    late = now - float(r.get("at") or 0)
                    head = ("到点了" if late < 60 else
                            "刚才说的那个时间过了")
                    _say(head + "：%s" % body)
        except Exception as e:
            print("[提醒] 循环出错（不影响聊天）：%s" % str(e)[:80], flush=True)
        time.sleep(_TICK)


def _say(text):
    """到点开口。走主动搭话那条路，App 轮询会弹通知。"""
    from runtime import _LOCAL, API_CFG
    from proactive import _cloud_append_assistant, _in_quiet, _proactive_conf
    pa = _proactive_conf()
    if _in_quiet(pa.get("quiet_start", "23:00"), pa.get("quiet_end", "07:00")):
        # 静默时段不硬闯，但必须留痕：删掉就等于这个提醒被吞了
        _save(_load() + [{"at": time.time() + 60 * 60,
                          "body": str(text)[:40], "src": "defer",
                          "t": time.strftime("%Y-%m-%d %H:%M")}])
        print("[提醒] 静默时段，推迟一小时再说", flush=True)
        return False
    b = _LOCAL.get("brain")
    if b is None:
        return False
    with _LOCAL["lock"]:
        ans, _mode = b.chat(
            "[系统指令] 你刚才答应他到点提醒一件事，现在到点了，你要主动发一条消息。\n"
            "[提醒内容] %s\n[回复规则] 用你平时的口气，一两句，别写成通知公告，"
            "也别解释你为什么记着这件事。" % text,
            proactive=True, log=False)
    ans = str(ans or "").strip()
    if not ans or ans == "无":
        return False
    from reply import strip_say_marker
    ans = strip_say_marker(ans)
    _cloud_append_assistant(ans)
    print("[提醒] 她主动提醒了：%s" % ans[:50], flush=True)
    return True


def start(brain):
    threading.Thread(target=_loop, daemon=True).start()
