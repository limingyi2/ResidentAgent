# -*- coding: utf-8 -*-
"""历史摘要（分层）—— 把滑出窗口的旧对话压成"聊过什么"，让她不忘这几天。

为什么要有它
-----------
2026-09-29 用户问："历史清理有没有方法换成别的？比如上下文压缩，
她不会忘了这几天聊过的。"

之前的机制是**丢弃式**的：hist 在内存里只留 24 条，超出从头部硬丢，
丢掉的**不变成任何东西**；磁盘 chat_history.jsonl 永久保留，但**从不读回**
（只有重启时 seed_history 灌最后 16 条）。实测云端 530 条 / 12,680 字 / 跨 10 天，
聊天时她一条都看不见 —— 所以"昨天聊的今天就忘"。

三层结构（必须分层，因为只有日摘要的话，摘要自己会变成新的负担：
90 天就是 90 段 × 120 字 = 10,800 字，跟固定骨架一样沉）：

    最近 7 天   → 日摘要   data/summary/YYYY-MM-DD.md
    8 ~ 30 天   → 周摘要   data/summary/rollup/YYYY-Www.md
    30 天以上   → 月摘要   data/summary/rollup/YYYY-MM.md

总量钉在约 2,000 字，**不随天数增长**（实测：分层方案长期稳定在 ¥0.015/轮，
而"原文全塞"到第 90 天是 ¥0.29/轮，32 倍）。

水位线
-----
data/summary/_state.json 里的 `pressed_upto` = 已压过的最后一行行号。
chat_history.jsonl 是 append-only，行号稳定；文件被清空或截断时
（总行数 < 水位线）自动复位重来，不会漏也不会重复。

跟记忆库（memory_store_v2）的分工
--------------------------------
记忆库抽的是**事实**（"他养了只猫叫年糕"），是点状的、跟话题无关；
这里记的是**聊过什么**（话题、他的近况、没说完的事、当时的氛围），
是线状的、按时间排。两者互不替代。

设计上的保守处（因为摘要由模型生成，模型会编）
--------------------------------------------
1. 压缩 prompt 明令"只写记录里确实出现过的，不许推断补充"；
2. 原始 chat_history.jsonl **一条都不删** —— 任何时候都能重新压；
3. API 不可用时降级成截断拼接，绝不因为摘要故障影响聊天；
4. 注入时明说"这些是真发生过的，但别当刚发生的事讲、别补细节" ——
   防她把摘要里的旧事当成此刻发生的事（这正是日记块踩过的坑）。
"""
import datetime
import json
import os
import re
import threading
import time

import httpx

try:
    import paths
    CHAT_LOG = paths.CHAT_LOG
    SUMMARY_DIR = paths.SUMMARY_DIR
except Exception:                      # 单独跑这个文件时的兜底
    _HERE = os.path.dirname(os.path.abspath(__file__))
    CHAT_LOG = os.path.join(_HERE, "data", "chat_history.jsonl")
    SUMMARY_DIR = os.path.join(_HERE, "data", "summary")

ROLLUP_DIR = os.path.join(SUMMARY_DIR, "rollup")
STATE_PATH = os.path.join(SUMMARY_DIR, "_state.json")

# ---- 压缩口径 ----
KEEP_RECENT = 24         # 最近这么多行不压（= 12 个来回，正好是窗口里的原话）
BATCH_MAX = 240          # 一次 tick 最多压多少行（防止首次跑太久）
DAY_TARGET = 120         # 单天摘要目标字数
WEEK_LIMIT = 250         # 周摘要上限
MONTH_LIMIT = 320        # 月摘要上限
BLOCK_CAP = 1500         # 注入块总字数上限
DAY_KEEP = 7             # 7 天内读日摘要
WEEK_KEEP = 30           # 30 天内读周摘要
WEEK_MAX = 4             # 最多带几段周摘要
MONTH_MAX = 2            # 最多带几段月摘要
CACHE_TTL = 60           # block() 的内存缓存秒数
LOOP_MAX = 8             # 一次 tick 最多循环几批（首次补压积压时用，防止占太久）

_LOCK = threading.Lock()
_CACHE = {"t": 0.0, "text": ""}

# ---------------------------------------------------------------- prompt

COMPRESS_PROMPT = """你是记忆助手。下面是"他"和"她"的一段聊天记录。
请把它压成一段简短的回顾，让她之后还能接上这个话头。

规则：
1. 只写记录里**确实出现过**的内容。绝对不许推断、补充、联想，
   也不要把没说的话写成说过了。宁可写得少。
2. 优先记这四类：① 聊了什么话题 ② 他提到自己的哪些近况／计划／烦恼
   ③ 有没有说好但还没做完的事 ④ 当时的气氛（轻松／他心情不好／话少）
3. 不要逐句复述，抓主干。不要评价、不要总结升华。
4. 用"他"指代对方，{target} 字以内，一段话，不要分点、不要标题。
5. 如果这段只是打招呼、或者没什么实质内容，就只输出两个字：无

聊天记录：
{body}
"""

MERGE_PROMPT = """你是记忆助手。下面是同一个来源的几段旧回顾，
请把它们并成一段更短的回顾（越老的越可以只留一句主干）。

规则：
1. 只做合并和压缩，**不许新增任何内容**，不许推断。
2. 保留具体的事（谁做了什么、什么安排），删掉细节和语气。
3. {target} 字以内，一段话，不要分点、不要标题。

几段旧回顾：
{body}
"""


def _target_word():
    return "她"


# ---------------------------------------------------------------- 基础 IO

def _ensure():
    try:
        os.makedirs(ROLLUP_DIR, exist_ok=True)
    except OSError:
        pass


def _read_text(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_text(p, text):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
        return True
    except OSError:
        return False


def _append_text(p, text):
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        return True
    except OSError:
        return False


def _list_files(d, pattern):
    try:
        return sorted(n for n in os.listdir(d)
                      if re.match(pattern, n) and os.path.isfile(os.path.join(d, n)))
    except OSError:
        return []


def _day_path(day):
    return os.path.join(SUMMARY_DIR, day + ".md")


# ---------------------------------------------------------------- 聊天存档

def _read_log():
    """读 chat_history.jsonl，返回 [(行号, 时间, 角色, 正文)]（行号从 1 起）"""
    if not os.path.exists(CHAT_LOG):
        return []
    out = []
    try:
        with open(CHAT_LOG, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                txt = (r.get("text") or "").strip()
                if not txt:
                    continue
                out.append((i, (r.get("t") or "").strip(),
                            r.get("role") or "user", txt))
    except OSError:
        return []
    return out


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_state(s):
    try:
        _ensure()
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def pending_rows(rows, upto):
    """还没压、且已经滑出窗口的行。返回 (选中行, 新的水位线)

    批次按**天**对齐：不在一批里把某一天截成两半 —— 否则同一天会被压成
    两段摘要，话题在中间断掉，读起来就是"上半截没说完就换了件事"。
    """
    if not rows:
        return [], 0
    total = rows[-1][0]
    if upto > total:                   # 文件被清空/换过 → 复位
        upto = 0
    end = total - KEEP_RECENT
    if end <= upto:
        return [], upto
    sel = [r for r in rows if upto < r[0] <= end]
    if len(sel) <= BATCH_MAX:
        return sel, end
    cut, cnt, last_day = 0, 0, None
    for r in sel:
        d = r[1][:10] or "未知"
        if d != last_day:
            if cnt >= BATCH_MAX:
                break
            last_day = d
        cnt += 1
        cut += 1
    if cut == 0:                       # 某一天自己就超了上限 —— 只能切
        sel = sel[:BATCH_MAX]
    else:
        sel = sel[:cut]
    return sel, (sel[-1][0] if sel else upto)


# ---------------------------------------------------------------- 调模型

def _ask(api, prompt, timeout=90, max_tokens=500):
    """调一次模型。任何失败都返回 ""，绝不抛。"""
    if not api or not api.get("api_key"):
        return ""
    try:
        r = httpx.post(
            (api.get("api_base") or "").rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api["api_key"]},
            json={"model": api.get("model", "deepseek-chat"),
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": max_tokens,
                  "reasoning_effort": "low"},
            timeout=timeout,
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return ""


def _squeeze(text, limit):
    """保险丝：模型偶尔会超字数，硬截到 limit 并补个句号。"""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for ch in "。！？.!?":
        p = cut.rfind(ch)
        if p >= limit * 0.6:
            return cut[:p + 1]
    return cut.rstrip("，,、；;") + "…"


# ---------------------------------------------------------------- 压缩

def compress_day(day, items, api, timeout=90):
    """把某天的若干条对话压成一段。返回 "" 表示没压出东西。"""
    body = "\n".join("%s：%s" % ("他" if role == "user" else "她", txt)
                     for _, _, role, txt in items)
    # 太短的片段不值得花一次请求：直接取实况摘要
    if len(body) < 60:
        return ""
    prompt = COMPRESS_PROMPT.format(target=DAY_TARGET, body=body[:8000])
    txt = _ask(api, prompt, timeout=timeout, max_tokens=400)
    if not txt:
        return ""
    txt = re.sub(r"^```.*?```$", "", txt, flags=re.S).strip()
    if txt.replace("。", "").strip() in ("", "无"):
        return ""
    return _squeeze(txt, DAY_TARGET * 2)


def _merge(body, api, limit, timeout=90):
    """把几段旧摘要并成一段。API 不可用就退化成截断拼接。"""
    prompt = MERGE_PROMPT.format(target=limit, body=body[:8000])
    txt = _ask(api, prompt, timeout=timeout, max_tokens=600)
    if not txt:
        return _squeeze(body.replace("\n", " "), limit)
    return _squeeze(txt, limit)


def _drop(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def rollup(api):
    """老的日摘要 → 周摘要；老的周摘要 → 月摘要。

    **并完之后把源文件删掉**（不是留着）。

    为什么必须删 —— 2026-09-29 用真实存档回放发现两个问题，都出在"不删"上：

    1. 重复：同一天既留在日摘要里、又进了周摘要。日摘要在 DAY_KEEP 天
       边界上（第 7 天）会被升级，而 block() 读日摘要的范围还含第 7 天 ——
       实测 923 字的注入块里有约 170 字是同一件事说了两遍。
    2. 丢内容（更糟）：周文件一旦生成就 `if exists: continue` 不再更新。
       可跨周时，周内某天是后来才变老、才被压出来的 —— 它既进不了那个
       已存在的周文件，又会因为"有周文件"被 block 跳过。两边都不在。

    删源文件之后，`summary/*.md` 天然只剩"还没升级的"，与 rollup 不重叠，
    也与"跨周边界"无关。日摘要只是派生物 —— 原始 chat_history.jsonl
    一条不动，随时能重压，回溯性不受影响。
    """
    today = datetime.date.today()
    made = {"week": 0, "month": 0}

    # ---- 日 → 周：7 天以外的 ----
    by_week = {}
    for name in _list_files(SUMMARY_DIR, r"^\d{4}-\d{2}-\d{2}\.md$"):
        try:
            d = datetime.date.fromisoformat(name[:-3])
        except ValueError:
            continue
        if (today - d).days < DAY_KEEP:
            continue
        iso = d.isocalendar()
        by_week.setdefault("%04d-W%02d" % (iso[0], iso[1]), []).append(
            (d.isoformat(), name))
    for wk, items in sorted(by_week.items()):
        wp = os.path.join(ROLLUP_DIR, wk + ".md")
        pieces = []
        for day, _ in sorted(items):
            body = _read_text(_day_path(day)).replace("\n", " ")
            if body:
                pieces.append("%s：%s" % (day, body))
        if not pieces:
            continue
        # 已有周摘要就并进去，不覆盖 —— 那个周可能先前进过一批
        old = _read_text(wp)
        if old:
            pieces.append("（更早并进来的）" + old.replace("\n", " "))
        if _write_text(wp, _merge("\n".join(pieces), api, WEEK_LIMIT)):
            for _, name in items:
                _drop(os.path.join(SUMMARY_DIR, name))
            made["week"] += 1

    # ---- 周 → 月：30 天以外的 ----
    by_month = {}
    for name in _list_files(ROLLUP_DIR, r"^\d{4}-W\d{2}\.md$"):
        y, w = name[:-3].split("-W")
        try:
            d = datetime.date.fromisocalendar(int(y), int(w), 1)
        except (ValueError, AttributeError):
            continue
        if (today - d).days < WEEK_KEEP:
            continue
        by_month.setdefault(d.strftime("%Y-%m"), []).append((d.isoformat(), name))
    for mk, items in sorted(by_month.items()):
        mp = os.path.join(ROLLUP_DIR, mk + ".md")
        pieces = []
        for _, name in sorted(items):
            body = _read_text(os.path.join(ROLLUP_DIR, name)).replace("\n", " ")
            if body:
                pieces.append(body)
        if not pieces:
            continue
        old = _read_text(mp)
        if old:
            pieces.append("（更早并进来的）" + old.replace("\n", " "))
        if _write_text(mp, _merge("\n".join(pieces), api, MONTH_LIMIT)):
            for _, name in items:
                _drop(os.path.join(ROLLUP_DIR, name))
            made["month"] += 1

    return made


# ---------------------------------------------------------------- 主入口

def tick(api=None, verbose=True, max_seconds=300):
    """跑一次检查：压掉新滑出窗口的对话 + 滚动老摘要。

    幂等：跑第二次不会再压同样的行（水位线记着）。
    首次运行会把积压的全补上（一批 240 行，循环最多 LOOP_MAX 轮），
    所以整个 tick 有时间上限，防止一次占太久。
    任何异常都在内部吞掉（调用方是后台线程，不该拖垮服务）。
    """
    with _LOCK:
        _ensure()
        t0 = time.time()
        state = _load_state()
        upto = int(state.get("pressed_upto") or 0)
        segs, chars, lines = 0, 0, 0
        for _ in range(LOOP_MAX):
            if time.time() - t0 > max_seconds:
                break
            rows = _read_log()
            if not rows:
                break
            sel, new_upto = pending_rows(rows, upto)
            if not sel:
                break
            by_day = {}
            for r in sel:
                by_day.setdefault((r[1][:10] or "未知"), []).append(r)
            for day, items in sorted(by_day.items()):
                txt = compress_day(day, items, api)
                if txt and _append_text(_day_path(day), txt):
                    segs += 1
                    chars += len(txt)
            # 水位线一次推到底：那天压出"无"也算处理过了，
            # 不然每次 tick 都会重压同一段（而且花的是真钱）。
            upto = new_upto
            lines += len(sel)
        if lines:
            state["pressed_upto"] = upto
            state["last_run"] = time.strftime("%Y-%m-%d %H:%M")
            _save_state(state)
        made = rollup(api)
        _CACHE["t"] = 0.0                      # 有新内容，缓存作废
        if verbose and (segs or made["week"] or made["month"]):
            print("[摘要] 压了 %d 段 / %d 字（%d 条记录），周摘要 +%d、月摘要 +%d"
                  % (segs, chars, lines, made["week"], made["month"]), flush=True)
        return {"pressed": segs, "chars": chars, "lines": lines,
                "week": made["week"], "month": made["month"]}


# ---------------------------------------------------------------- 注入

def _week_label(name):
    """'2026-W39' → '9月21日那周'（拿那一周的周一算）"""
    try:
        y, w = name[:-3].split("-W")
        d = datetime.date.fromisocalendar(int(y), int(w), 1)
        return "%d月%d日那周" % (d.month, d.day)
    except Exception:
        return name


def block(cap=BLOCK_CAP, use_cache=True):
    """拼成给模型的注入块。没有摘要就返回 ""。

    顺序按时间正序（最近的放最后）—— 离当前对话越近，模型越看得住。
    """
    now = time.time()
    if use_cache and _CACHE["text"] and (now - _CACHE["t"]) < CACHE_TTL:
        return _CACHE["text"]

    today = datetime.date.today()
    entries = []                      # (排序键, 标签, 正文)

    # 月摘要（最旧，最多 2 段）
    for name in _list_files(ROLLUP_DIR, r"^\d{4}-\d{2}\.md$")[-MONTH_MAX:]:
        txt = _read_text(os.path.join(ROLLUP_DIR, name))
        if txt:
            y, m = name[:-3].split("-")
            entries.append((name, "%d年%d月" % (int(y), int(m)), txt))

    # 周摘要（最多 4 段）
    for name in _list_files(ROLLUP_DIR, r"^\d{4}-W\d{2}\.md$")[-WEEK_MAX:]:
        txt = _read_text(os.path.join(ROLLUP_DIR, name))
        if txt:
            entries.append((name, _week_label(name), txt))

    # 日摘要（最近 7 天，含今天早些时候的）
    for i in range(DAY_KEEP, -1, -1):
        d = today - datetime.timedelta(days=i)
        txt = _read_text(_day_path(d.isoformat()))
        if not txt:
            continue
        if i == 0:
            lab = "今天早些时候"
        elif i == 1:
            lab = "昨天"
        else:
            lab = "%d月%d日" % (d.month, d.day)
        entries.append((d.isoformat(), lab, txt))

    if not entries:
        _CACHE["t"], _CACHE["text"] = now, ""
        return ""

    entries.sort(key=lambda e: e[0])
    lines, used = [], 0
    for _, lab, txt in entries:
        seg = "· %s：%s" % (lab, txt.replace("\n", " "))
        if used + len(seg) > cap:
            break
        lines.append(seg)
        used += len(seg)
    if not lines:
        _CACHE["t"], _CACHE["text"] = now, ""
        return ""

    text = ("【你们之前聊过的（真发生过的事，可以自然地接上这些话题；"
            "但别当成刚发生的事讲，也别补充这里没写的细节）】\n"
            + "\n".join(lines))
    _CACHE["t"], _CACHE["text"] = now, text
    return text


def stats():
    """排查用：现在有几段摘要、水位线在哪"""
    days = _list_files(SUMMARY_DIR, r"^\d{4}-\d{2}-\d{2}\.md$")
    weeks = _list_files(ROLLUP_DIR, r"^\d{4}-W\d{2}\.md$")
    months = _list_files(ROLLUP_DIR, r"^\d{4}-\d{2}\.md$")
    st = _load_state()
    blk = block(use_cache=False)
    return {"day_files": len(days), "week_files": len(weeks),
            "month_files": len(months), "pressed_upto": st.get("pressed_upto", 0),
            "block_chars": len(blk)}
