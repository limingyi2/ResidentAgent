# -*- coding: utf-8 -*-
"""功能开关：App 设置页远程控制各附加功能的开/关，存 config.json 的 features 段。

读缓存 30 秒 —— 聊天链路每条消息都会问，不能每条都读一次盘；关掉后最多
30 秒生效。任何读取失败一律当"开"：开关系统自己坏了不能拖累功能。

DEFAULTS 里除核心功能外，插件声明了 FEATURE 的也会登记进来（见 plugin_host），
这样设置页不用为每个插件改代码，插件删了开关自然消失。
"""
import json
import time

try:
    from paths import CONFIG_PATH
    _CFG = str(CONFIG_PATH)
except Exception:
    _CFG = r"C:\linzhixia\config\config.json"

# 核心开关与默认值（插件的由 plugin_host._sync_features 追加）
DEFAULTS = {
    "hotboard": True,         # 热搜进她的世界
    "weather": True,          # 天气进她的世界
    "weather_alert": True,    # 天气变化主动提醒
    "packages": True,         # 快递监控与提醒
    "rand_img_chat": True,    # 聊天里她可以甩 [rand:] 图
    "rand_img_moment": True,  # 朋友圈配图优先用随机图
    "tools": True,            # 模型可自选调免费 API（查他在哪/天气/快递）
}

_cache = {"t": 0.0, "d": dict(DEFAULTS)}


def invalidate():
    """丢掉 30 秒缓存（插件登记完新开关、或刚写完 config 时调）。"""
    _cache["t"] = 0.0


def register(key, default=True):
    """登记一个开关（插件装载时调）。已存在不改默认值 —— config 里已有的设置优先。"""
    key = str(key or "").strip()
    if not key or key in DEFAULTS:
        return False
    DEFAULTS[key] = bool(default)
    invalidate()
    return True


def all_features():
    """当前全部开关（30 秒缓存）。"""
    if time.time() - _cache["t"] > 30:
        try:
            cfg = json.load(open(_CFG, encoding="utf-8"))
            f = cfg.get("features") or {}
            d = dict(DEFAULTS)
            d.update({k: bool(f[k]) for k in d if k in f})
            _cache["d"] = d
            _cache["t"] = time.time()
        except Exception:
            pass
    return dict(_cache["d"])


def on(key):
    """某功能开没开。不认识的 key 一律当开。"""
    return bool(all_features().get(key, True))


def set_features(patch):
    """App 改开关：合并写回 config.json，返回更新后的全部开关；写失败返回 None。"""
    try:
        cfg = json.load(open(_CFG, encoding="utf-8"))
        f = cfg.get("features") or {}
        for k, v in (patch or {}).items():
            if k in DEFAULTS:
                f[k] = bool(v)
        cfg["features"] = f
        with open(_CFG, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
        invalidate()
        return all_features()
    except Exception:
        return None
