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

    三种"发不出"要分开对待（用户看到的体感完全不同）：
      · 分类不在册 / 功能开关关了 → 静默删标签。这是规则不让发，不是故障。
      · 图库没响应               → 删标签 + 补一句"图库没响应"。静默删的话
        用户只看到她啥也没发，会以为她不想发。宁可多一句提示。
    残留标签绝不能漏给用户（App 侧就是 404 裂图）。
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
        if n:
            return "[img:%s]" % n
        # 拉不到图时**不能只把标签删干净**：正文原样发出去，用户看到的就是
        # "她啥也没发" —— 分不清是她不想发、还是图库没响应。补一句说明，
        # 让他知道下次可以再要一次。分类不在册/开关关掉的情况仍然静默删标签
        # （那不是故障，是规则不让发，说出来反而像在找借口）。
        return "\n（图库没响应，没发出去）"
    out = RAND_TAG_RE.sub(_sub, text)

    # 收尾清扫残缺标签（"[rand"、"[img:rand" 这种半截的）。
    #
    # 这里有个坑：不能靠正则的形态去区分"残留"和"合法"——
    # 生成的图片标签恰恰是 [img:rand_20261002_abcd.jpg]，文件名以 rand 开头，
    # 任何含 `rand` 的宽口径正则都会把它当成残留删掉，于是**图拉到了也发不出去**
    # （"她从来不发表情包"就是这个原因，且必现不是偶发）。
    #
    # 所以改成"先把生成好的标签换成占位符 → 清洗 → 再换回来"：
    # 清洗时正文里没有任何 rand 字样，绝无误伤；占位符用数字，不会被标签正则碰到。
    _ph = {}

    def _stash(m):
        k = "\x00%d\x00" % len(_ph)
        _ph[k] = m.group(0)
        return k

    out = re.sub(r"\[img[:：][^\]]*\]", _stash, out, flags=re.IGNORECASE)
    # 现在清 rand 残留，怎么宽都不会碰到合法图片标签了。
    # 字符类排除 \n：残缺的 "[rand" 后面如果直接换行再跟一个标签，
    # 用 [^\]]* 会一路吞到下一个 ]，把后面那张图的标签一起吃掉（实测会）。
    # 只在**同一行内**清，且要求紧跟冒号或直接闭合，避免误吞正常文字。
    out = re.sub(r"\[(?:img[:：]\s*)?rand(?:[:：][^\]\n]*)?\]?",
                 "", out, flags=re.IGNORECASE)
    for k, v in _ph.items():
        out = out.replace(k, v)
    return re.sub(r" {2,}", " ", out).strip()
