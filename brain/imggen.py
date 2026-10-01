# -*- coding: utf-8 -*-
"""生图：自拍 / [gen:描述] / 朋友圈配图最终都走这里。

两条铁律（实测踩出来的）：
- 参考图（她本人的照片）是"脸像不像"的唯一依据，且**提示词里绝对不要描写长相**
  —— 一描述模型就照文字自己造脸，参考图等于白喂。
- 负面词只压画质/畸形这类硬问题，不堆风格标签（风格词会把模型拽离参考图）。
"""
import os
import json
import time

from runtime import CFG_PATH

# 生图统一后缀：让她发的照片像"手机随手拍"，不是 AI 画
GEN_NEGATIVE = ("畸形手指，多余手指，多肢体，融合的手指，扭曲五官，不对称眼睛，"
                "模糊，低分辨率，水印，文字，签名")


def _cloud_gen_image(prompt, model=None, negative_prompt=None, size=None,
                     image=None):
    """用硅基流动生一张图，存进 upload 目录，返回文件名。

    negative_prompt：压"AI 味"的关键，默认 GEN_NEGATIVE，显式传 "" 可关掉。
    size：自拍用 768x1024 竖图更像手机随手拍；哪个尺寸不认就退回 1024x1024 ——
    别让一张图因为尺寸参数整个挂掉。
    image：参考图，**必须是 data URI 字符串**（传数组会报 "image should be a
    string"）。传了就是图生图，用来锁住她的长相。这条路不通会自动丢掉图重试。
    """
    import base64 as _b64
    import urllib.request as _u

    try:
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        cfg = {}
    # 生图模型：App「模型」页选的那个优先，没选过就用下面这个默认
    model = model or cfg.get("image_model") or "Tongyi-MAI/Z-Image-Turbo"
    neg = GEN_NEGATIVE if negative_prompt is None else negative_prompt
    size = size or "1024x1024"
    key = str(cfg.get("api_key") or "")
    base = str(cfg.get("api_base") or "https://api.siliconflow.cn/v1").rstrip("/")
    if not key:
        return None

    def _try(sz, img=None):
        payload = {"model": model, "prompt": prompt, "image_size": sz}
        if neg:
            payload["negative_prompt"] = neg
        if img:
            payload["image"] = img
        body = json.dumps(payload).encode("utf-8")
        req = _u.Request(base + "/images/generations", data=body, method="POST",
                         headers={"Authorization": "Bearer " + key,
                                  "Content-Type": "application/json"})
        try:
            op = _u.build_opener(_u.ProxyHandler({}))
            return json.loads(op.open(req, timeout=180).read().decode("utf-8")), op
        except Exception as e:
            print(f"[大脑] 生图失败（{sz}）：{str(e)[:80]}", flush=True)
            return None, None

    d, op = _try(size, image)
    if d is None and image:                        # 参考图这条路不通：丢掉图再试
        d, op = _try(size, None)
    if d is None and size != "1024x1024":          # 尺寸不认就退回方形再来
        d, op = _try("1024x1024")
    if d is None:
        return None
    url = ""
    try:
        url = (d.get("images") or [{}])[0].get("url") or ""
    except Exception:
        url = ""
    if not url:
        b64 = ""
        try:
            b64 = (d.get("images") or [{}])[0].get("b64_json") or ""
        except Exception:
            b64 = ""
        if not b64:
            return None
        blob = _b64.b64decode(b64)
    else:
        try:
            blob = op.open(url, timeout=120).read()
        except Exception:
            return None
    try:
        from paths import UPLOAD_DIR
        d = str(UPLOAD_DIR)
    except Exception:
        d = r"C:\linzhixia\data\upload"
    os.makedirs(d, exist_ok=True)
    name = "gen_" + time.strftime("%Y%m%d_%H%M%S") + ".png"
    with open(os.path.join(d, name), "wb") as f:
        f.write(blob)
    return name


# --- 她的标准长相 ---
# 实测：参考图只给脸（上半身裁切）效果最好，整张沙滩照会把海景一起带进结果
_LOOK_CACHE = {"path": "", "uri": None}


def _look_ref_path():
    """她标准长相参考图的路径。找不到返回空串（生图不能被它带崩）。"""
    cands = []
    try:
        from paths import CONFIG_DIR, ROOT
        cands += [os.path.join(str(CONFIG_DIR), "look_refs"),
                  os.path.join(str(ROOT), "look_refs")]
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.path.join(here, "look_refs"),
              os.path.join(here, "config", "look_refs"),
              r"C:\linzhixia\look_refs"]
    for d in cands:
        # prepared/ 是本机整理参考图时的暂存子目录，正式放法是与 look_refs 平铺
        # （云端就是平铺），这里多认一层，免得两种放法有一种读不到。
        for base in (d, os.path.join(d, "prepared")):
            if not os.path.isdir(base):
                continue
            for pref in ("look_face", "look_half", "look_full"):   # 先要"只有脸"那张
                for ext in (".jpg", ".jpeg", ".png"):
                    p = os.path.join(base, pref + ext)
                    if os.path.isfile(p):
                        return p
            for n in sorted(os.listdir(base)):                     # 兜底：任何 look_*
                p = os.path.join(base, n)
                if n.lower().startswith("look_") and os.path.isfile(p):
                    return p
    return ""


def _look_ref_uri():
    """参考图的 data URI（读一次缓存住 —— 别每张照片都重读盘 + 重新编码）。"""
    if _LOOK_CACHE["uri"]:
        return _LOOK_CACHE["uri"]
    try:
        import base64 as _b64
        p = _look_ref_path()
        if not p:
            print("[大脑] 没找到她的长相参考图（look_refs/），自拍只能退回文字描述",
                  flush=True)
            return None
        mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"
        with open(p, "rb") as f:
            uri = "data:%s;base64,%s" % (mime, _b64.b64encode(f.read()).decode())
        _LOOK_CACHE["path"] = p
        _LOOK_CACHE["uri"] = uri
        print("[大脑] 已加载她的长相参考图：" + os.path.basename(p), flush=True)
        return uri
    except Exception as e:
        print(f"[大脑] 长相参考图读取失败：{str(e)[:80]}", flush=True)
        return None


# 锁脸这句有两个要点：
#   1) 必须是"照参考图本人画"。写成"生成一个像照片里这样的女孩"就废了 ——
#      模型会重新画一个，参考图白给。
#   2) 只留长相本身的锚点（脸小 / 眼睛大 / 皮肤白 / 甜美），不写发型、不写
#      任何风格词 —— 发型交给参考图，风格词会让模型偏离参考图
_LOOK_LOCK = ("照参考图里这个女孩本人画，就是她本人：脸小、眼睛大而清亮、"
              "皮肤白皙、长相甜美，长相和发型都跟参考图保持一致，不要换脸。")


def _cloud_gen_selfie(scene, size="768x1024", negative_prompt=None):
    """给她**本人**的照片：先用参考图锁脸，行不通再退回纯文字描述。

    scene 只写画面（在哪、在干嘛、穿什么、什么光线），**不要写长相**。
    """
    ref = _look_ref_uri()
    if ref:
        n = _cloud_gen_image(_LOOK_LOCK + scene, negative_prompt=negative_prompt,
                             size=size, image=ref)
        if n:
            return n
    look = _her_look()
    return _cloud_gen_image(((look + "，" + scene) if look else scene),
                            negative_prompt=negative_prompt, size=size)


def _her_look():
    """她的人设外貌描述。**现在只是兜底**：自拍默认用参考图锁脸，只有参考图缺失
    或调用失败时才退回文字描述生图。
    """
    try:
        from runtime import _LOCAL
        return str((_LOCAL["brain"].persona or {}).get("appearance") or "")
    except Exception:
        return ""


def _sticker_path(name):
    """文件名 -> 本地路径（stickers.py 惰性加载，出错给空串）。"""
    try:
        import stickers
        return stickers.path_of(name)
    except Exception:
        return ""


def _cloud_sticker(name):
    """表情包 / 聊天存档图片的二进制（App 要显示图）。找不到返回 None。

    name 既可以是表情包库文件名，也可以是聊天记录里存的图片路径
    （绝对路径，或 data 目录下的相对路径）。
    """
    try:
        p = _sticker_path(name)
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    try:
        from paths import DATA_DIR
        data_root = os.path.abspath(str(DATA_DIR))
        seen = set()
        for c in (name, os.path.basename(name)):
            if not c or c in seen:
                continue
            seen.add(c)
            cands = [c if os.path.isabs(c) else os.path.join(data_root, c),
                     os.path.join(data_root, "upload", c)]
            for cp in cands:
                cand = os.path.abspath(cp)
                if cand.startswith(data_root) and os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        return f.read()
    except Exception:
        pass
    return None
