# -*- coding: utf-8 -*-
"""热搜缓存：跟天气同一套路 —— 后台每小时刷，聊天链路只读缓存，绝不现拉。"""
import io
import json
import os
import time

try:
    import paths
    _CACHE = os.path.join(str(paths.DATA_DIR), "hotboard.json")
except Exception:
    _CACHE = r"C:\linzhixia\data\hotboard.json"

MAX_AGE = 6 * 3600
KEEP = 10               # 缓存里留 10 条，注入提示词时取前 5


def refresh(platform, token):
    import uapi
    lst = uapi.fetch_hotboard(platform, token)
    if not lst:
        return False
    tmp = _CACHE + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"t": time.time(), "platform": platform,
                   "titles": [str(x.get("title") or "") for x in lst[:KEEP]]},
                  f, ensure_ascii=False)
    os.replace(tmp, _CACHE)
    return True


def note(n=5):
    """给提示词用的热搜标题串；没有或过期返回空串。"""
    try:
        d = json.load(io.open(_CACHE, encoding="utf-8"))
        if time.time() - float(d.get("t") or 0) > MAX_AGE:
            return ""
        titles = [t for t in (d.get("titles") or []) if t][:n]
        return "、".join(titles)
    except Exception:
        return ""
