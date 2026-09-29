# -*- coding: utf-8 -*-
"""表情包素材库：她收藏的图，回复时自己决定要不要甩出来。

设计原则（别做成死板程序）：
- 素材主要从聊天自然积累 —— 你发过的表情包她会收藏（梗是你们俩的），
  也可以往 data/stickers/ 里手动丢图。
- 每张图配一句视觉模型的描述，她在 prompt 里能看到每张是什么，
  自己挑、自己决定发不发 —— 不写触发规则，不按概率随机。

目录：data/stickers/
索引：data/stickers/index.json —— {文件名: {"desc":…, "src":…, "t":…}}
"""
import datetime
import hashlib
import json
import os
import threading
import time

import paths

_DIR = paths.STICKERS_DIR
_lock = threading.Lock()
_index = None                       # 惰性加载


def _ensure_loaded():
    global _index
    if _index is None:
        try:
            os.makedirs(_DIR, exist_ok=True)
            _index = json.load(open(os.path.join(_DIR, "index.json"),
                                    encoding="utf-8"))
        except Exception:
            _index = {}
    return _index


def _save_index():
    try:
        json.dump(_index, open(os.path.join(_DIR, "index.json"), "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
    except OSError:
        pass


def _sniff_ext(blob):
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:4] == b"GIF8":
        return "gif"
    return ""


def save_bytes(blob, desc="", src=""):
    """收藏一张表情包，返回文件名；失败返回 ""。"""
    if not blob:
        return ""
    idx = _ensure_loaded()
    ext = _sniff_ext(blob)
    if not ext:
        return ""
    h = hashlib.md5(blob).hexdigest()[:8]
    name = f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{h}.{ext}"
    try:
        with open(os.path.join(_DIR, name), "wb") as f:
            f.write(blob)
    except OSError:
        return ""
    with _lock:
        idx[name] = {"desc": (desc or "")[:80], "src": src,
                     "t": time.strftime("%Y-%m-%d %H:%M")}
        _save_index()
    return name


def path_of(name):
    """文件名 -> 绝对路径；不存在/非法返回 ""。"""
    idx = _ensure_loaded()
    name = os.path.basename(str(name or "").strip())
    if not name or name not in idx:
        return ""
    p = os.path.join(_DIR, name)
    return p if os.path.exists(p) else ""


def all_names():
    return list(_ensure_loaded().keys())


def block(max_n=12):
    """给 draft prompt 的素材清单。一张都没有时返回 ""。"""
    idx = _ensure_loaded()
    if not idx:
        return ""
    items = sorted(idx.items(), key=lambda kv: kv[1].get("t", ""),
                   reverse=True)[:max_n]
    lines = []
    for name, v in items:
        desc = v.get("desc") or "（内容未知）"
        lines.append(f"- {name}：{desc}")
    head = (f"【你收藏的表情包】一共 {len(idx)} 张（下面是最新的 {len(items)} 张）。"
            "想配图就在回复的单独一行写 [img:文件名]——只有聊天软件能把这行"
            "变成图片，别写在别处；能不能用、用哪张，你自己掂量，别硬凑：")
    return head + "\n" + "\n".join(lines)
