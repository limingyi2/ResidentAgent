# -*- coding: utf-8 -*-
"""随机图（UAPI 图库）：她甩表情包 / 朋友圈配图都用它。"""
import os
import re
import time
import random

RAND_TAG_RE = re.compile(r"\[rand[:：]\s*([^\]]+?)\s*\]")
# 只挡明确不许发的，其余一律放行 —— 接口给什么就发什么。
# 早先这里是份正向白名单（只有 6 个分类），模型选错一个就把标签静默删掉，
# 她那边成了"啥也没发"。而她手上没有菜单以外的任何信息，选错是必然的。
# 现在改成黑名单：她看到的分类说明（persona_store.py）就是接口真有的，
# 提示词加一类不用回来改这里。
# furry 图库真实存在（实测 302），用户明令屏蔽，必须留在挡的这边。
_RAND_BLOCK = ("furry",)
# 壁纸只在他点名"壁纸"时才给 —— 他不说就是不要
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
    残留标签不能漏给用户（App 侧就是 404 裂图）。
    模型会把标签写成 [img:rand:bq] 这种嵌套变体，先归一化再解析（忽略大小写）。
    """
    text = re.sub(r"\[img[:：]\s*rand[:：]\s*([^\]]+?)\s*\]", r"[rand:\1]",
                  (text or ""), flags=re.IGNORECASE)

    def _sub(m):
        cat = (m.group(1) or "").strip().lower()
        if cat in _RAND_BLOCK:
            return ""
        if cat in _RAND_WALLPAPER and "壁纸" not in (user_text or ""):
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
        # 拉不到图时不能只把标签删干净。正文原样发出去用户看到的就是
        # "她啥也没发"，分不清是她不想发还是图库没响应。被屏蔽的分类才静默删。
        return "\n（图库没响应，没发出去）"
    out = RAND_TAG_RE.sub(_sub, text)

    # 收尾清扫残缺标签（"[rand"、"[img:rand" 这种半截的）。
    # 不能靠正则形态区分"残留"和"合法"：生成的图片标签恰恰是
    # [img:rand_20261002_abcd.jpg]，文件名以 rand 开头，任何含 rand 的宽口径
    # 正则都会把它当残留删掉，图拉到了也发不出去（必现不是偶发）。
    # 改成"先把生成好的标签换成占位符 → 清洗 → 再换回来"：清洗时正文里没有
    # 任何 rand 字样，绝无误伤；占位符用数字，不会被标签正则碰到。
    _ph = {}

    def _stash(m):
        k = "\x00%d\x00" % len(_ph)
        _ph[k] = m.group(0)
        return k

    out = re.sub(r"\[img[:：][^\]]*\]", _stash, out, flags=re.IGNORECASE)
    # 字符类排除 \n：残缺的 "[rand" 后面直接换行再跟一个标签的话，
    # [^\]]* 会一路吞到下一个 ]，把后面那张图的标签一起吃掉。
    # 只在同一行内清，且要求紧跟冒号或直接闭合。
    out = re.sub(r"\[(?:img[:：]\s*)?rand(?:[:：][^\]\n]*)?\]?",
                 "", out, flags=re.IGNORECASE)
    for k, v in _ph.items():
        out = out.replace(k, v)
    return drop_missing_imgs(re.sub(r" {2,}", " ", out).strip())


# 已经变成 rand_2026xxxx_xxxx.jpg / 真实素材名的标签，得确认文件真在。
_IMG_TAG_RE = re.compile(r"\[img[:：]([^\]\n]+?)\s*\]")


def drop_missing_imgs(text):
    """把指向不存在图片的 [img:名字] 删掉，防 App 裂图。

    模型偶尔凭空编文件名（实测见过 [img:无语.jpg] —— 清单里没有"无语.jpg"，
    它把描述当成了文件名）。这种发出去就是 404 裂图。

    **只对形态确定的素材名动手**（表情包库是 时间戳_md5.ext），其余一律放过。
    宁可漏删也不误删：少发一张图只是少个表情，误删几次她就不敢发了。
    """
    def _sub(m):
        name = (m.group(1) or "").strip()
        if not name:
            return ""
        # rand_/gen_ 是刚落盘的生成图，文件必然在（能走到这一步就说明拉成功了）
        if name.startswith("rand_") or name.startswith("gen_"):
            return m.group(0)
        # 表情包库文件名固定是 时间戳_md5.ext。**其余形态一律放过** ——
        # 模型偶尔会把描述写成文件名（实测 [img:无语.jpg]），但宁可放过也不
        # 误删：她要是发现发图总被吞，下次就不发了。
        if not re.match(r"^\d{8}_\d{6}_[0-9a-f]{8}\.\w+$", name):
            return m.group(0)
        try:
            import imggen
            if imggen._cloud_sticker(name):
                return m.group(0)
            return ""
        except Exception:
            return m.group(0)
    return _IMG_TAG_RE.sub(_sub, text or "")
