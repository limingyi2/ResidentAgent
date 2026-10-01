# -*- coding: utf-8 -*-
"""天气缓存：每小时由后台循环刷一次，聊天链路只读这个文件，绝不现拉。

城市跟着**他的 IP** 走 —— 人在哪天气就是哪，IP 定不出来退回 world.json 的 city。
触发规则也在这个文件里：雨/雪/高低温/大风只报一次（一天一档），事件 reminder 用。
"""
import io
import json
import os
import re
import time

try:
    import paths
    _CACHE = os.path.join(str(paths.DATA_DIR), "weather.json")
    _ALERT = os.path.join(str(paths.DATA_DIR), "weather_alert.json")
except Exception:
    _CACHE = r"C:\linzhixia\data\weather.json"
    _ALERT = r"C:\linzhixia\data\weather_alert.json"

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


def refresh_auto(ip, token, fallback_city):
    """城市跟着他的 IP 走。IP 没变就复用上次定位的城市（一天顶多定位几次）。

    IP 定位失败就不更新记录里的 ip —— 下个小时同一个 ip 会再试，城市先用旧的顶着。
    """
    try:
        d = json.load(io.open(_CACHE, encoding="utf-8"))
    except Exception:
        d = {}
    city, located = "", False
    if ip:
        if d.get("ip") == ip and d.get("city"):
            city = str(d["city"])
        else:
            import uapi
            city = uapi.fetch_ip_city(ip, token) or ""
            located = bool(city)
    # 没定位到新的就沿用上次的；再没有才退回 world.json 的城市
    city = city or str(d.get("city") or "") or fallback_city or ""
    if city:
        refresh(city, token)
    if located or city:
        try:
            d = json.load(io.open(_CACHE, encoding="utf-8"))   # refresh 刚写的，重读防覆盖
        except Exception:
            d = {}
        if located:
            d["ip"] = ip
        d["city"] = city
        try:
            tmp = _CACHE + ".new"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, _CACHE)
        except Exception:
            pass
    return city


def city():
    """缓存里定位到的城市（可能为空）。"""
    try:
        return str(json.load(io.open(_CACHE, encoding="utf-8")).get("city") or "")
    except Exception:
        return ""


def facts():
    """触发规则要的原始数据：{weather, temp, wind}。没有或过期返回 None。"""
    try:
        d = json.load(io.open(_CACHE, encoding="utf-8"))
        if time.time() - float(d.get("t") or 0) > MAX_AGE:
            return None
        try:
            t = float(d.get("temperature"))
        except Exception:
            t = None
        return {"city": str(d.get("city") or ""), "weather": str(d.get("weather") or ""),
                "temp": t, "wind": str(d.get("wind") or "")}
    except Exception:
        return None


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


# --- 触发规则：代码判档位，模型只管语气 ---

def check_trigger():
    """按当前天气算要不要提醒他。命中返回 (事件短句, 天气详情)，不命中 None。

    一天一档只报一次（落盘去重）：上午下雨提醒过，下午接着下就不再说第二遍。
    """
    f = facts()
    if not f:
        return None
    w, t = f["weather"], f["temp"]
    event, detail = None, ""
    if "雨" in w:
        event, detail = "下雨了", "%s %s℃" % (w, f["temp"])
    elif "雪" in w:
        event, detail = "下雪了", "%s %s℃" % (w, f["temp"])
    elif t is not None and t >= 35:
        event, detail = "高温", "%s %s℃" % (w, f["temp"])
    elif t is not None and t <= 5:
        event, detail = "降温", "%s %s℃" % (w, f["temp"])
    elif f["wind"] and re.search(r"[6-9]级|1[0-9]级", f["wind"]):
        event, detail = "大风", "%s %s" % (w, f["wind"])
    if not event:
        return None
    today = time.strftime("%Y-%m-%d")
    try:
        a = json.load(io.open(_ALERT, encoding="utf-8"))
    except Exception:
        a = {}
    if a.get("date") == today and event in (a.get("done") or []):
        return None
    done = (a.get("done") or []) if a.get("date") == today else []
    done.append(event)
    try:
        tmp = _ALERT + ".new"
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as f2:
            json.dump({"date": today, "done": done}, f2, ensure_ascii=False)
        os.replace(tmp, _ALERT)
    except Exception:
        pass
    return event, detail
