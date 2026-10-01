# -*- coding: utf-8 -*-
"""天气缓存：每小时由后台循环刷一次，聊天链路只读这个文件，绝不现拉。"""
import io
import json
import os
import time

try:
    import paths
    _CACHE = os.path.join(str(paths.DATA_DIR), "weather.json")
except Exception:
    _CACHE = r"C:\linzhixia\data\weather.json"

# 缓存超过 6 小时就当没有（宁可她不提天气，别把前天的雨说成今天的）
MAX_AGE = 6 * 3600


def refresh(city, token):
    """拉一次并落盘。任何失败静默 —— 缓存里留着旧的到 6 小时自然过期。"""
    import uapi
    d = uapi.fetch_weather(city, token)
    if not d:
        return False
    tmp = _CACHE + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"t": time.time(), "city": city, "weather": d.get("weather"),
                   "temperature": d.get("temperature"), "wind": d.get("wind_power"),
                   "humidity": d.get("humidity")}, f, ensure_ascii=False)
    os.replace(tmp, _CACHE)
    return True


def note():
    """给提示词用的一句天气；没有或过期返回空串。"""
    try:
        d = json.load(io.open(_CACHE, encoding="utf-8"))
        if time.time() - float(d.get("t") or 0) > MAX_AGE:
            return ""
        bits = [str(d.get("weather")), "%s℃" % d.get("temperature")]
        if d.get("wind"):
            bits.append(str(d["wind"]))
        if d.get("humidity") is not None:
            bits.append("湿度%s%%" % d["humidity"])
        return " ".join(x for x in bits if x and x != "None℃")
    except Exception:
        return ""
