# -*- coding: utf-8 -*-
"""约定账本 —— 把"说好但还没到的事"从语义检索里拎出来，按日期强制注入。

为什么要有它
-----------
2026-09-29 用户描述了两类问题，方向其实是一个：

  1. "我让她国庆来找我玩" → 这事被压成记忆库里的一条 event，没错；
  2. "我后面提到快到国庆了、终于要快见到你了，她应该自动提取出这件事" ——
     实测做不到，或者说**要靠运气**。

因为记忆库是"语义命中才带"的：Top-5 排序里，一句话只要没撞上关键词就往
下掉。实测（拿云端 34 条真实记忆复现）：
    假期跟调休是不是连着放 → 那条约定排第 2 名 → 能召回
    放假了还上早八         → 排第 13 名 → 召不回（只取前 5）
    票买了吗（5 个字）      → 压根不检索（太短，跳过）
也就是说：**同一件事，换个说法就漏。**

约定跟其它记忆根本不是一类东西 —— 它是**有时间的**。有日期就不该靠语义
碰运气，应该按时间窗口无条件带上：只要快到那天了，她无论聊什么都能想起
"我跟他约好 1 号见"。这就是这个模块的全部职责。

跟其它两套的分工
---------------
    memory_store_v2  抽事实（"他养了只猫叫年糕"）—— 语义检索，点状
    recap_store      记聊过什么（话题、氛围）—— 按时间全量带，线状
    agenda（本模块） 记**约好的事**（"25 号她去找他"）—— 按日期强制带，定时

设计上的保守处（解析日期很容易出错，错了比没有更糟）
--------------------------------------------------
1. **只对 type=event 的条目做解析** —— fact 是长期属性，没有日期。
2. **解析不出来就不进账本**，宁可漏；不猜测、不用模型补。
3. 支持的说法是一张白名单（X月Y日 / Y号 / 明天后天 / 下周X / 国庆 / 周末 /
   月底 / ISO 日期），白名单之外一律不算。
4. 原始 memory.json **一个字段都不改** —— 这个模块只读它、另写自己那份
   data/agenda.json，随时能整个删掉重来。
"""
import datetime
import json
import os
import re
import time

try:
    import paths
    AGENDA_PATH = os.path.join(paths.DATA_DIR, "agenda.json")
    DEFAULT_MEM = os.path.join(paths.MEMORY_DIR, "memory.json")
except Exception:                       # 单独跑这个文件时的兜底
    _HERE = os.path.dirname(os.path.abspath(__file__))
    AGENDA_PATH = os.path.join(_HERE, "data", "agenda.json")
    DEFAULT_MEM = os.path.join(_HERE, "data", "memory", "memory.json")

PAST_WINDOW = 14         # 过去这么多天内的还带（**要标注已过去**，见 _label）
FUTURE_WINDOW = 7        # 未来这么多天内的提前带（快到了就该想起来）
PRUNE_DAYS = 60          # 超过这么多天的旧条目清掉（文件别无限长）
BLOCK_CAP = 600          # 注入块字数上限
CACHE_TTL = 60           # block() 内存缓存秒数

_CACHE = {"t": 0.0, "text": ""}

# ---- 日期表达白名单（顺序即优先级）----
_RE_ISO = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?")
_RE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*[日号](?!\d)")
_RE_WEEKDAY = re.compile(r"下{1,2}(?:周|星期|礼拜)([一二三四五六日天])")
_REL = (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0), ("今晚", 0))
_WEEK_CN = "一二三四五六日天"


# ---------------------------------------------------------------- 日期解析

def parse_date(text, base):
    """从一句话里解析出事件日期。解析不出返回 None。

    base 是这句话**说出来的那天**（用记忆条目的 time 字段），因为"25 号"
    "明天"这类都是相对当时说的。
    """
    if not text or not isinstance(base, datetime.date):
        return None

    # 1) 完整日期：2026-09-28 / 2026年9月25日
    m = _RE_ISO.search(text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    # 2) X月Y日 / X月Y号
    m = _RE_MD.search(text)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        return _fix_year(base, mo, d)

    # 3) 相对词：明天 / 后天 / 大后天
    for k, off in _REL:
        if k in text:
            return base + datetime.timedelta(days=off)

    # 4) 下周三 / 下下周三（周一算第一天）
    m = _RE_WEEKDAY.search(text)
    if m:
        ch = m.group(1)
        wd = 6 if ch == "天" else _WEEK_CN.index(ch)
        nxt_mon = base + datetime.timedelta(days=(7 - base.weekday()))
        return nxt_mon + datetime.timedelta(days=wd)

    # 5) 国庆（固定 10-01）
    if "国庆" in text:
        return _fix_year(base, 10, 1)

    # 6) 月底 / 月末
    if "月底" in text or "月末" in text:
        nxt = base.replace(day=28) + datetime.timedelta(days=4)
        return nxt - datetime.timedelta(days=nxt.day)

    # 7) 周末 / 这周末（算本周六）
    if "周末" in text:
        return base + datetime.timedelta(days=(5 - base.weekday()) % 7)

    # 8) 只说"Y号"，不带月份 —— 最含糊，放最后
    m = _RE_DAY.search(text)
    if m:
        d = int(m.group(1))
        if not 1 <= d <= 31:
            return None
        dt = _fix_year(base, base.month, d)
        if dt is None:
            return None
        # 说的时候已经过去一周以上 → 说的是下个月
        if dt < base - datetime.timedelta(days=7):
            mo2 = base.month + 1
            y2 = base.year + (1 if mo2 > 12 else 0)
            dt = _fix_year(base, 1 if mo2 > 12 else mo2, d)
            if dt is not None and dt.year < y2:
                dt = None                       # 跨年补不上就算了，不硬猜
        return dt

    return None


def _fix_year(base, mo, d):
    """按 base 的年月组装日期；跨年（说的时候已过去很久）自动推到明年。"""
    try:
        dt = datetime.date(base.year, mo, d)
    except ValueError:
        return None
    if dt < base - datetime.timedelta(days=200):
        try:
            dt = datetime.date(base.year + 1, mo, d)
        except ValueError:
            return None
    return dt


def _base_date(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 存取

def _load():
    try:
        with open(AGENDA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("items")
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _save(items):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(AGENDA_PATH)), exist_ok=True)
        with open(AGENDA_PATH, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def _load_mem_items(mem_path):
    try:
        with open(mem_path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("items") or []
    except Exception:
        return []


# ---------------------------------------------------------------- 主流程

def refresh(mem_path=None, verbose=False):
    """扫记忆库的 events，把能解析出日期的写进 agenda.json。幂等。

    只读 memory.json，绝不改它 —— 解析错了最多是这个账本多一条废话，
    删掉文件就干净了，不会污染她真正的记忆。
    """
    items = _load_mem_items(mem_path or DEFAULT_MEM)
    if not items:
        return {"added": 0, "pruned": 0, "total": len(_load())}

    known = _load()
    # 源记忆被删掉（比如手工清理记忆库）时，账本里的那条影子也要跟着走 ——
    # 否则会出现"她记得一件记忆库里根本没有的事"（2026-09-29 清理时发现：
    # 删了记忆库里的"国庆期间将与对方见面"，账本却还留着它继续注入）。
    alive = {(it.get("id") or "") for it in items}
    kept = [k for k in known if not k.get("src") or k.get("src") in alive]
    pruned = len(known) - len(kept)
    known = kept
    have = {(it.get("text"), it.get("date")) for it in known}
    added = 0
    for it in items:
        if (it.get("type") or "") != "event":
            continue
        text = (it.get("text") or "").strip()
        if not text or len(text) > 200:
            continue                       # 太长的多半是模型写的总结，不解析
        base = _base_date(it.get("time"))
        if not base:
            continue
        d = parse_date(text, base)
        if not d:
            continue
        key = (text, d.isoformat())
        if key in have:
            continue
        known.append({"text": text, "date": d.isoformat(),
                      "src": it.get("id") or "",
                      "added": time.strftime("%Y-%m-%d %H:%M")})
        have.add(key)
        added += 1

    # 清掉很久以前的，别让文件无限长
    today = datetime.date.today()
    keep = []
    for it in known:
        try:
            d = datetime.date.fromisoformat(it.get("date") or "")
        except ValueError:
            pruned += 1
            continue
        if (today - d).days > PRUNE_DAYS:
            pruned += 1
            continue
        keep.append(it)

    if added or pruned:
        _save(keep)
        _CACHE["t"] = 0.0
    if verbose and (added or pruned):
        print("[约定] 新增 %d 条、清理 %d 条，账本共 %d 条"
              % (added, pruned, len(keep)), flush=True)
    return {"added": added, "pruned": pruned, "total": len(keep)}


def _label(d, today):
    """日期标签（方向由分组标题表达，这里只说距离）"""
    diff = (d - today).days
    if diff == 0:
        return "今天"
    if diff == 1:
        return "明天"
    if diff == -1:
        return "昨天"
    if diff < 0:
        return "%d月%d日（%d 天前）" % (d.month, d.day, -diff)
    return "%d月%d日（%d 天后）" % (d.month, d.day, diff)


def block(cap=BLOCK_CAP, today=None, use_cache=True):
    """拼成注入块。窗口内没有约定就返回 ""。"""
    now = time.time()
    if use_cache and _CACHE["text"] and (now - _CACHE["t"]) < CACHE_TTL:
        return _CACHE["text"]

    today = today or datetime.date.today()
    lo = today - datetime.timedelta(days=PAST_WINDOW)
    hi = today + datetime.timedelta(days=FUTURE_WINDOW)

    sel = []
    for it in _load():
        try:
            d = datetime.date.fromisoformat(it.get("date") or "")
        except ValueError:
            continue
        if lo <= d <= hi:
            sel.append((d, (it.get("text") or "").strip()))
    sel = [(d, t) for d, t in sel if t]
    if not sel:
        _CACHE["t"], _CACHE["text"] = now, ""
        return ""

    sel.sort(key=lambda x: x[0])
    future = [(d, t) for d, t in sel if d >= today]
    past = [(d, t) for d, t in sel if d < today]

    lines, used = [], 0
    for title, items in (("还没到的：", future),
                         ("已经过完的日子（别再当约好的事提）：", past)):
        if not items or used + len(title) > cap:
            if items:
                break
            continue
        lines.append(title)
        used += len(title)
        full = False
        for d, t in items:
            seg = "· %s：%s" % (_label(d, today), t)
            if used + len(seg) > cap:
                full = True
                break
            lines.append(seg)
            used += len(seg)
        if full:
            break
    if not lines:
        _CACHE["t"], _CACHE["text"] = now, ""
        return ""

    # 「今天是 X月X日」必须写在块里，不能只指望 system 最前面那句时间提示 ——
    # 隔了 4000 多字，模型的注意力早散了。日期紧贴条目，才建立得起
    # "9月25日 = 4 天前" 这种关系（2026-09-29 实测：不写这句她就把
    # 已经过完的 25 号当成还没到的安排，一直说"到时候见"）。
    wk = "一二三四五六日"[today.weekday()]
    text = ("【日程】今天是 %d月%d日（周%s）。他提过的事都在下面，别装不知道。"
            "分两段看：“还没到的”是往后要办的；“已经过完的日子”是指那天"
            "已经过去了 —— 提它要用“那次”“当时”这种过去说法，"
            "别说“到时候见”、别当成还没到。别主动加码承诺新的。\n"
            % (today.month, today.day, wk) + "\n".join(lines))
    _CACHE["t"], _CACHE["text"] = now, text
    return text


def stats(today=None):
    """排查用：账本里有什么、哪些在窗口内"""
    today = today or datetime.date.today()
    items = _load()
    lo = today - datetime.timedelta(days=PAST_WINDOW)
    hi = today + datetime.timedelta(days=FUTURE_WINDOW)
    inwin = []
    for it in items:
        try:
            d = datetime.date.fromisoformat(it.get("date") or "")
        except ValueError:
            continue
        if lo <= d <= hi:
            inwin.append((d.isoformat(), it.get("text")))
    inwin.sort()
    return {"total": len(items), "in_window": inwin,
            "block_chars": len(block(use_cache=False, today=today))}
