# -*- coding: utf-8 -*-
"""随机图（UAPI 图库）：她甩表情包 / 朋友圈配图都用它。"""
import os
import re
import time
import random

RAND_TAG_RE = re.compile(r"\[rand[:：]\s*([^\]]+?)\s*\]")
# 她能主动甩的分类。furry 图库存在但**永远不发**（用户明令屏蔽）；
# 壁纸类只在他说了"壁纸"时才给 —— 没点名就当没这个类
_RAND_OK = ("bq", "acg", "landscape", "anime", "general_anime", "ai_drawing")
_RAND_WALLPAPER = ("pc_wallpaper", "mobile_wallpaper")


def _cloud_random_image(category):
    """从 UAPI 图库拉一张图存进 upload，返回文件名；失败返回空串。"""
    try:
        import uapi
        from runtime import API_CFG
        data = uapi.fetch_random_image(
            category, (API_CFG.get("uapi") or {}).get("token") or "")
        if not data:
            return ""
        from paths import UPLOAD_DIR
        d = str(UPLOAD_DIR)
        os.makedirs(d, exist_ok=True)
        name = "rand_%s_%s.jpg" % (time.strftime("%Y%m%d_%H%M%S"),
                                   "".join(random.choice("0123456789abcdef")
                                           for _ in range(4)))
        with open(os.path.join(d, name), "wb") as f:
            f.write(data)
        return name
    except Exception:
        return ""


def resolve_rand_tags(text, user_text=""):
    """[rand:分类] → 从图库拉一张现成图，换成 [img:文件名]。

    分类不在册 / 功能开关关了 / 图拉不到，都把标签删干净，原文绝不能漏给用户。
    模型会把标签写成 [img:rand:bq] 这种嵌套变体，先归一化再解析（忽略大小写）。
    """
    text = re.sub(r"\[img[:：]\s*rand[:：]\s*([^\]]+?)\s*\]", r"[rand:\1]",
                  (text or ""), flags=re.IGNORECASE)
    def _sub(m):
        cat = (m.group(1) or "").strip().lower()
        if cat in _RAND_WALLPAPER:
            if "壁纸" not in (user_text or ""):
                return ""
        elif cat not in _RAND_OK:
            return ""
        try:
            import features
            if not features.on("rand_img_chat"):
                return ""
        except Exception:
            pass
        n = _cloud_random_image(cat)
        return ("[img:%s]" % n) if n else ""
    out = RAND_TAG_RE.sub(_sub, (text or ""))
    # 兜底：任何形态的 rand 残留（怪变体、半截标签）一律整段删掉，
    # 存档里漏一个原样标签，App 就是 404 裂图（03:43 那次"图片消失"的根源）
    out = re.sub(r"\[(?:img[:：]\s*)?rand[^]]*\]", "", out, flags=re.IGNORECASE)
    # 删完标签留下的连续空格收成一格，行首尾的空白直接去掉
    return re.sub(r" {2,}", " ", out).strip()
