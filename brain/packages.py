# -*- coding: utf-8 -*-
"""快递监控：他提一嘴单号就进监控，后台每 30 分钟查一次，有进展她主动说一嘴。

数据在 data/packages.json。项目全程用 JSON 存档，这里也一样 —— 单号就几个，
引 SQLite 反而多余。查询失败静默（下一轮再查），绝不影响聊天。
"""
import io
import json
import os
import re
import time

try:
    import paths
    _DB = os.path.join(str(paths.DATA_DIR), "packages.json")
except Exception:
    _DB = r"C:\linzhixia\data\packages.json"

POLL_SEC = 30 * 60

# 要主动提醒的状态（其余状态只更新记录不出声）。派送中提醒一次、签收提醒一次、
# 异常提醒一次；提醒过就记在 last_notified，同状态不重复说
_NOTIFY_ON = ("out_for_delivery", "delivered", "exception")

# 单号定位不能用 \b：中文是 \w，「号SF123…」里"号"和"S"之间没有边界。
# 用环视：两侧都不是字母数字才算完整单号
_NUM_RE = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]{1,2}\d{10,15}|\d{12,18})(?![0-9A-Za-z])")
# 没有关键词不算 —— 防止把手机号、订单日期当单号
_KW_RE = re.compile(r"快递|单号|物流|包裹|到货|发货|收货|顺丰|圆通|中通|韵达|申通|极兔|德邦|邮政|EMS|京东")


def extract_tracking(text):
    """从他的话里找出快递单号（可能没有）。"""
    if not _KW_RE.search(text or ""):
        return []
    out, seen = [], set()
    for m in _NUM_RE.finditer(text):
        no = m.group(1).upper()
        if no not in seen:
            seen.add(no)
            out.append(no)
    return out


def _load():
    try:
        return json.load(io.open(_DB, encoding="utf-8"))
    except Exception:
        return {"packages": []}


def _save(d):
    tmp = _DB + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DB)


def register(number, token):
    """把单号加进监控（已在就顺手刷新状态）。返回记录 dict，查不到返回 None。"""
    import uapi
    d = uapi.fetch_tracking(number, token)
    if not d or not d.get("tracking_number"):
        return None
    rec = {"number": number.upper(),
           "carrier": str(d.get("carrier_name") or "快递"),
           "status": str(d.get("status") or "未知"),
           "status_code": str(d.get("status_code") or "unknown"),
           "latest": str((d.get("tracks") or [{}])[0].get("context") or ""),
           "is_completed": bool(d.get("is_completed")),
           "added_at": time.strftime("%Y-%m-%d %H:%M"),
           "last_check": time.time(),
           "last_notified": ""}
    db = _load()
    for old in db["packages"]:
        if old["number"] == rec["number"]:
            old.update({k: rec[k] for k in
                        ("carrier", "status", "status_code", "latest",
                         "is_completed", "last_check")})
            _save(db)
            return old
    db["packages"].append(rec)
    _save(db)
    return rec


def status_line(rec):
    """给提示词注入用的一句话现状。"""
    note = ("（%s）" % rec["note"]) if rec.get("note") else ""
    return "%s 单号 %s，当前状态：%s%s%s" % (
        rec.get("carrier"), rec.get("number"), rec.get("status"), note,
        ("；最新：%s" % rec["latest"]) if rec.get("latest") else "")


def list_all():
    """全部监控中的单号（App 快递页用），按加入时间倒序。"""
    return sorted(_load()["packages"],
                  key=lambda r: r.get("added_at") or "", reverse=True)


def remove(number):
    """移出监控（App 里删了，她的提醒也就跟着停了 —— poll 只扫这份存档）。"""
    db = _load()
    n = len(db["packages"])
    db["packages"] = [p for p in db["packages"] if p["number"] != number]
    if len(db["packages"]) == n:
        return False
    _save(db)
    return True


def set_note(number, note):
    """改备注（App 快递页的「编辑」）。"""
    db = _load()
    for p in db["packages"]:
        if p["number"] == number:
            p["note"] = (note or "").strip()[:30]
            _save(db)
            return p
    return None


def poll(token):
    """查一遍所有没签收的单号。返回要提醒的事件列表 [{rec, event}]。

    只有状态**变到**提醒档才出声：刚加进来就是"派送中"也算一次（他得知道），
    连着两轮都是派送中就闭嘴。签收的单子提醒完标记完事，之后不再查。
    """
    import uapi
    db = _load()
    events = []
    for rec in db["packages"]:
        if rec.get("is_completed") and rec.get("last_notified") == "delivered":
            continue
        d = uapi.fetch_tracking(rec["number"], token)
        rec["last_check"] = time.time()
        if not d:
            continue
        code = str(d.get("status_code") or "unknown")
        rec.update({"carrier": str(d.get("carrier_name") or rec["carrier"]),
                    "status": str(d.get("status") or rec["status"]),
                    "status_code": code,
                    "latest": str((d.get("tracks") or [{}])[0].get("context")
                                  or rec.get("latest") or ""),
                    "is_completed": bool(d.get("is_completed"))})
        if code in _NOTIFY_ON and rec.get("last_notified") != code:
            rec["last_notified"] = code
            events.append({"rec": rec, "event": code})
    _save(db)
    return events
