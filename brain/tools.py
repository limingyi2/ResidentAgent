# -*- coding: utf-8 -*-
"""模型的工具箱：她可以自己决定调免费 API 查实时数据。

用法在 persona 规则里教：回复里单独一行写 [tool:名] 或 [tool:名:参数]，
server 执行后把结果作为系统提示再喂回给她，她重新组织语言回答（最多两轮）。
标签绝不能漏进给用户看的正文 —— run_chat_with_tools 末尾负责兜底清扫。
查询失败不装懂，工具函数返回 None，提示里就让她照实说"查不到"。
"""
import re

# 全角冒号一起吃（模型爱混用）；参数段可有可无
TOOL_TAG_RE = re.compile(
    r"\[tool[:：]([A-Za-z_]+)(?:[:：]([^\]]*))?\]", re.IGNORECASE)

# 兜底清扫用的宽口径：只要看着像 [tool:...] 就扫掉，名字不限字符集。
# 执行用上面那个严的（名字必须在册），清扫用这个宽的 —— 模型会编名字甚至编个
# 中文名（[tool:不存在:xx]），匹配不到就会原样发到用户眼前
TOOL_TAG_ANY_RE = re.compile(r"\[tool[:：][^\]]*\]", re.IGNORECASE)


def _uapi_token():
    from runtime import API_CFG
    return (API_CFG.get("uapi") or {}).get("token") or ""


def _his_ip_now():
    try:
        from proactive import _his_ip
        return _his_ip() or ""
    except Exception:
        return ""


def _locate():
    """他在哪的唯一入口：IP 现查优先，失败退天气缓存。
    返回 (城市, ip, 是否缓存值)；ip 为空说明他没用手机连过来。"""
    from uapi import fetch_ip_city
    ip = _his_ip_now()
    if ip:
        city = fetch_ip_city(ip, _uapi_token())
        if city:
            return city, ip, False
    try:
        import weather_cache
        return weather_cache.city(), ip, True
    except Exception:
        return "", ip, True


def _t_ip_city(arg):
    """他在哪：拿他最近的公网 IP 现查归属地（快递地址 ≠ 人在哪）。"""
    city, ip, cached = _locate()
    if not ip and not city:
        return "查不到：他还没用手机连过来，没有 IP 可定位"
    if city:
        tag = "IP实时定位" if not cached else "最近一次缓存定位（可能过期）"
        return "他的手机当前%s在：%s" % (tag, city)
    return "IP定位查不到，别猜，让他自己说他现在在哪儿"


def _t_weather(arg):
    """某地实时天气；不带参数就自动定位他在的城市。"""
    from uapi import fetch_weather
    city = (arg or "").strip()
    if not city:
        city, _ip, _c = _locate()
    if not city:
        return "查不到：连城市都没定位到，让他说一下他在哪儿"
    d = fetch_weather(city, _uapi_token())
    if not d:
        return "%s的天气查不到（接口失败），别编" % city
    bits = [str(d.get("weather") or "?"), "%s℃" % d.get("temperature")]
    if d.get("wind_direction"):
        bits.append("%s%s" % (d.get("wind_direction"), d.get("wind_power") or ""))
    if d.get("humidity") is not None:
        bits.append("湿度%s%%" % d.get("humidity"))
    return "%s现在：%s" % (city, "，".join(bits))


def _t_tracking(arg):
    """查某个快递单号的实时物流（不进监控，只答一次）。"""
    from uapi import fetch_tracking
    no = (arg or "").strip().upper()
    if not no:
        return "查不到：得给他单号"
    d = fetch_tracking(no, _uapi_token())
    if not d:
        return "单号 %s 查不到物流（可能没揽收或单号不对），照实说" % no
    tracks = d.get("tracks") or []
    latest = str(tracks[0].get("context")) if tracks else ""
    return "%s（%s）：%s%s" % (
        no, d.get("carrier_name") or "快递", d.get("status") or "未知",
        ("，最新一条：" + latest) if latest else "")


TOOLS = {
    "ip_city": _t_ip_city,
    "weather": _t_weather,
    "tracking": _t_tracking,
}

# 教给 persona 规则的一行清单（tools 规则在 persona_store 里，两处必须对得上）
PROMPT_LINE = ("[tool:ip_city] 查他现在的实时位置；"
               "[tool:weather:城市] 查某地实时天气（不写城市=自动定位他的）；"
               "[tool:tracking:快递单号] 查快递物流")


def run_tool(name, arg):
    """执行一个工具调用；名字不在册/执行失败都返回 None（让她说查不到）。"""
    fn = TOOLS.get(str(name or "").lower())
    if not fn:
        return None
    try:
        return fn(arg) or None
    except Exception:
        return None


def run_chat_with_tools(chat_fn, text, max_rounds=2):
    """带工具循环的聊天外层。

    chat_fn(t) -> (ans, mode)。她第一轮的回复里带 [tool:...] 就执行，
    结果拼进系统提示再问一轮（img 描述已在 text 里，第二轮不重看图）。
    返回 (最终回复, mode, 用过的工具名)，没用到工具第三个返回 ""。
    """
    used = ""
    ans, mode = chat_fn(text)
    for _ in range(max(0, max_rounds - 1)):
        m = TOOL_TAG_RE.search(ans or "")
        if not m:
            break
        used = m.group(1).lower()
        result = run_tool(used, m.group(2))
        hint = ("（系统提示：你刚才调用了工具[%s]，结果：%s。"
                "现在用这个结果直接回答他，别再写任何工具标签）"
                % (used, result if result else "查询失败，照实说查不到，别编"))
        ans, mode = chat_fn(text + "\n" + hint)
    # 兜底清扫：两轮后还残留的（或压根没触发循环的）不能漏给用户。
    # 这里用宽口径，连模型编出来的名字一起扫
    ans = TOOL_TAG_ANY_RE.sub("", ans or "").replace("  ", " ").strip()
    return ans, mode, used
