# -*- coding: utf-8 -*-
"""UAPI（uapis.cn）的两个 GET：实时天气 + 节假日万年历。

设计约束：聊天主链路**绝不依赖**它 —— 超时 3 秒、任何失败返回 None，
调用方拿不到就当没这个数据。token 在 config.json 的 uapi.token
（该文件已 gitignore，仓库是公开的，key 不能进仓库）。
认证是 query 参数 ?token=（实测 header Bearer 不认）。
"""
import json
import urllib.parse
import urllib.request

_BASE = "https://uapis.cn/api/v1"


def _get(path, params, token, timeout=3):
    url = _BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "&token=" + urllib.parse.quote(token))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_weather(city, token):
    """实时天气。够用的几个字段：weather/temperature/wind_direction/wind_power/humidity。"""
    try:
        return _get("/misc/weather", {"city": city}, token)
    except Exception:
        return None


def fetch_year_holidays(year, token):
    """某年的法定节假日 + 调休上班日（legal_rest / legal_workday_adjust），失败 None。"""
    try:
        d = _get("/misc/holiday-calendar", {"year": year, "holiday_type": "legal"}, token)
        return d.get("holidays") or []
    except Exception:
        return None


def fetch_hotboard(platform, token):
    """某平台实时热榜：[{title, hot_value, index, url}...]，失败 None。"""
    try:
        d = _get("/misc/hotboard", {"type": platform}, token)
        return d.get("list") or []
    except Exception:
        return None


def fetch_tracking(number, token, phone=""):
    """快递物流：{carrier_name, status, status_code, is_completed,
    tracks:[{time, context}]}（tracks 按时间倒序，tracks[0] 是最新），失败 None。"""
    p = {"tracking_number": number}
    if phone:
        p["phone"] = phone
    try:
        return _get("/misc/tracking/query", p, token)
    except Exception:
        return None


def fetch_ip_city(ip, token):
    """IP 归属地的城市名。region 形如「中国 山东省 烟台市」，取最后一段去掉「市」。
    定不准（境外/字段缺失）返回 None，调用方退回配置里的默认城市。"""
    try:
        d = _get("/network/ipinfo", {"ip": ip}, token)
        parts = [p for p in str(d.get("region") or "").split() if p]
        city = (parts[-1] if parts else "").strip().rstrip("市")
        return city or None
    except Exception:
        return None


def fetch_random_image(category, token, timeout=15):
    """随机图片：接口直接回图片二进制（302 到图床后返回 image/*）。
    成功返回 bytes，失败/返回的不是图返回 None。超时给足 —— 风景/二次元
    图一张几百 KB 到 2MB，云机 1Mbps 出网，8 秒会临界。"""
    url = _BASE + "/random/image?" + urllib.parse.urlencode({"category": category})
    req = urllib.request.Request(url + "&token=" + urllib.parse.quote(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = str(r.headers.get("Content-Type") or "")
            data = r.read()
        if data and ctype.startswith("image/"):
            return data
    except Exception:
        pass
    return None
