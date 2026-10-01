# -*- coding: utf-8 -*-
"""让她"看见"图片 —— 走云端视觉模型。

为什么走 API 而不是本地：本地那套视觉模块早就删了（8.3GB 权重），而且它是给
NVIDIA 卡准备的，这台是 AMD，跑不了。现在把图发到云端、拿回一句中文描述，
再把描述当普通文字喂进她的对话。

**她看不到像素，只看得到那句话。** 但对"你发了张照片给她看"来说这已经够了，
而且比让她瞎猜强得多。

成本：一张图大约 100~1500 token（取决于尺寸）。发之前会把长边压到 max_side，
手机随手拍的照片基本落在几分钱一张。模型在 config.json 的 vision 段可换。
"""
import os
import io
import json
import base64
import urllib.request
import urllib.error

DEFAULT_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
DEFAULT_MAX_SIDE = 1024

# 刻意强调"只说看到的"：视觉模型看到模糊的图很容易顺着猜，
# 猜出来的东西进了她的上下文就变成"事实"
DEFAULT_PROMPT = (
    "用一到两句话描述这张图片里能看到的东西，口语一点、像跟朋友说的。"
    "只说你确实看见的；看不清、不确定的地方就别提，不要猜。"
)


def _cfg(api_config):
    v = (api_config or {}).get("vision")
    return v if isinstance(v, dict) else {}


def enabled(api_config):
    return bool(_cfg(api_config).get("enabled", True))


def shrink(path, max_side=DEFAULT_MAX_SIDE, quality=82):
    """把图压小再送出去 —— 长边不超过 max_side。

    视觉模型的 token 主要花在图片上，尺寸直接决定花费。手机照片动辄 4000px，压到
    1024 长边后描述质量几乎没差别，费用差好几倍。压不动就原样返回。
    """
    try:
        with open(path, "rb") as f:
            return shrink_bytes(f.read(), max_side=max_side, quality=quality)
    except Exception:
        with open(path, "rb") as f:
            return f.read()


def shrink_bytes(blob, max_side=DEFAULT_MAX_SIDE, quality=82):
    """shrink 的二进制版本（QQ 桥收藏表情包时图还没落盘）。"""
    try:
        from PIL import Image, ImageOps
        import io
        img = Image.open(io.BytesIO(blob))
        img = ImageOps.exif_transpose(img)      # 手机竖拍的照片要按 EXIF 转正
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return blob


def describe(path, api_config, timeout=90):
    """调用云端视觉模型，返回一句中文描述。失败返回 ""（调用方当作"看不清"处理）。

    绝不抛异常 —— 图片描述失败不该让整条聊天链路崩掉。失败自动重试一次，
    平台上偶尔排队超时，重试往往第二次就成了。
    """
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return ""
    return describe_bytes(blob, api_config, timeout=timeout)


def describe_bytes(blob, api_config, timeout=90):
    """直接看一段图片二进制（QQ 桥收藏表情包时用）。"""
    if not blob:
        return ""
    desc = _describe_bytes_once(blob, api_config, timeout)
    if not desc:
        desc = _describe_bytes_once(blob, api_config, timeout)
    return desc


def _describe_once(path, api_config, timeout=90):
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return ""
    return _describe_bytes_once(blob, api_config, timeout)


def _describe_bytes_once(blob, api_config, timeout=90):
    v = _cfg(api_config)
    if not v.get("enabled", True):
        return ""
    base = str(api_config.get("api_base") or "").rstrip("/")
    key = str(api_config.get("api_key") or "")
    if not base or not key:
        return ""

    model = str(v.get("model") or DEFAULT_MODEL)
    prompt = str(v.get("prompt") or DEFAULT_PROMPT)
    try:
        max_side = int(v.get("max_side") or DEFAULT_MAX_SIDE)
    except (TypeError, ValueError):
        max_side = DEFAULT_MAX_SIDE

    try:
        raw = shrink_bytes(blob, max_side=max_side)
    except Exception:
        return ""
    b64 = base64.b64encode(raw).decode("ascii")

    payload = {
        "model": model,
        "max_tokens": int(v.get("max_tokens") or 200),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        # 强制直连、绕开一切代理（环境变量 + Windows 系统代理）：
        # 系统代理经常把大体积图片请求挂到超时，而直连一直正常
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return ""

    try:
        msg = d["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    text = (msg.get("content") or "").strip()
    if not text:
        # 有些模型把话写在 reasoning_content 里
        text = (msg.get("reasoning_content") or "").strip()
    # 去掉换行，她那边是一句话的输入
    return " ".join(text.split())[:400]
