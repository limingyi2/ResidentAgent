# -*- coding: utf-8 -*-
"""UAPI（uapis.cn）的几个 GET：天气 / 节假日 / 热榜 / 快递 / IP归属地 / 随机图。

设计约束：聊天主链路**绝不依赖**它 —— 超时 6 秒、任何失败返回 None，
调用方拿不到就当没这个数据。token 在 config.json 的 uapi.token
（该文件已 gitignore，仓库是公开的，key 不能进仓库）。
认证是 query 参数 ?token=（实测 header Bearer 不认）。
"""
import json
import time
import urllib.parse
import urllib.request

_BASE = "https://uapis.cn/api/v1"


def _get(path, params, token, timeout=6):
    """两次机会：UAPI 对背靠背调用偶发拖死连接（实测约 1/5 概率，
    紧接着重发一次 0.1 秒就回），都失败才把异常抛给上层转 None。"""
    url = _BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "&token=" + urllib.parse.quote(token))
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
    raise last


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


def fetch_random_image(category, token, timeout=8):
    """随机图片：接口直接回图片二进制（302 到图床后返回 image/*）。
    成功返回 bytes，失败/返回的不是图返回 None。

    **超时给 8 秒而不是更久**：实测这个接口约 25% 概率把连接挂死 —— 表现是
    一次数据都收不到、耗满整个 timeout 才抛；而成功的请求只要 0.2~0.4 秒。
    给 15 秒并不会让更多图拉下来，只会让每次失败多白等 7 秒（聊天主链路在等）。

    **必须重试**：只打一次的话，"她偶尔不发表情包"就是这个 —— 模型明明写了
    [rand:bq]，拉图失败后标签被清掉，她那边就成了"啥也没发"，他还以为她不想发。
    两次机会 + 间隔 0.4 秒（照 _get 的经验：背靠背重发立刻就能成）。
    最坏耗时 8×2+0.4 ≈ 16.4 秒，换来约 25% → 约 6% 的失败率。
    """
    url = _BASE + "/random/image?" + urllib.parse.urlencode({"category": category})
    req = urllib.request.Request(url + "&token=" + urllib.parse.quote(token))
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = str(r.headers.get("Content-Type") or "")
                data = r.read()
            if data and ctype.startswith("image/"):
                return data
            break        # 回来了但不是图（被限流/返回 JSON），重发也一样
        except Exception:
            if attempt == 0:
                time.sleep(0.4)   # 第一次挂死，立刻再打一次
                continue
    return None
