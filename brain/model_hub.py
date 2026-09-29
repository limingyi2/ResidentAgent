# -*- coding: utf-8 -*-
"""模型中枢：服务商、模型分类、当前配置的读写。

为什么单独抽一个文件
--------------------
模型调用原来散在三个地方，各读各的配置字段：

    brain.py        对话   读 config.json 的 model
    vision.py       看图   读 config.json 的 vision.model
    server.py      生图   _cloud_gen_image() 里写死 Z-Image-Turbo

App 想换个模型就得同时改三处，还容易漏。抽到这里之后：
"现在用哪个模型"只有一份定义，"这个名字是生图还是语言"只有一份规则。

四类模型（App 里就是这四个下拉）
------------------------------
    chat     对话   她说每句话用的
    vision   看图   他发图片时，把图翻译成一句中文给她看
    image    生图   她的自拍、朋友圈配图
    audio    语音   还没做，位置先留着（App 里置灰，标"还没做"）
    embed    向量   记忆检索用的，App 里不显示（她不需要你选）

分类从哪来：先问服务商 /v1/models 要真实列表，按名字关键字归类。
拉不到（没网 / key 错了 / 平台改版）就用下面的兜底清单，并在返回值里
标 source=fallback —— App 会提示"这是内置清单，可能不全"。
"""
import os
import json
import time
import urllib.request

try:
    import paths
    from paths import CONFIG_PATH, DATA_DIR
except Exception:                      # 单独跑这个文件时的兜底
    CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "config", "config.json")
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data")

CACHE_PATH = os.path.join(DATA_DIR, "run", "model_cache.json")
CACHE_TTL = 6 * 3600                   # 模型名单不会一天变三回，缓存 6 小时

# 服务商：后面要加别家（比如智谱、 moonshot），往这里加一条就行
PROVIDERS = {
    "siliconflow": {
        "name": "硅基流动",
        "base": "https://api.siliconflow.cn/v1",
        "models_doc": "https://docs.siliconflow.cn",
    },
    "custom": {
        "name": "自定义（OpenAI 兼容）",
        "base": "",
        "models_doc": "",
    },
}

# 分类关键字（都对小写后的模型 id 做包含匹配）。
# 坑：别用 "turbo" 判生图 —— Qwen 一堆 -Turbo 是语言模型，会全错分。
_IMAGE_HINTS = ("kolors", "flux", "stable-diffusion", "sdxl", "sd3",
                "z-image", "zimage", "hidream", "playground", "wanx",
                "dall-e", "gpt-image", "recraft", "kandinsky", "lumina",
                "imagen", "majicflus", "sd-turbo", "sana")
_AUDIO_HINTS = ("whisper", "sensevoice", "funaudio", "fish-speech",
                "fishspeech", "indextts", "maskgct", "step-tts", "tts",
                "asr", "voice", "audio", "speech", "emotion2vec")
_VISION_HINTS = ("-vl", "vl-", "vision", "internvl", "minicpm-v", "llava",
                 "glm-4v", "cogvlm", "omni", "qwen-vl", "yi-vl")
_EMBED_HINTS = ("bge-", "bce-", "gte-", "jina-", "embedding", "rerank",
                "reranker", "text-embedding")

# 兜底清单（2026-09 整理）。在线拉不到时给 App 用，至少能选。
FALLBACK = {
    "chat": [
        "Qwen/Qwen3-8B", "Qwen/Qwen3-14B", "Qwen/Qwen3-32B",
        "Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-72B-Instruct",
        "deepseek-ai/DeepSeek-V3", "deepseek-ai/DeepSeek-R1",
        "zai-org/GLM-4.6", "zai-org/GLM-5.3", "THUDM/glm-4-9b-chat",
        "meta-llama/Meta-Llama-3.1-8B-Instruct",
    ],
    "vision": [
        "Qwen/Qwen3-VL-8B-Instruct", "Qwen/Qwen3-VL-30B-A3B-Instruct",
        "Qwen/Qwen2.5-VL-32B-Instruct", "Qwen/Qwen2.5-VL-72B-Instruct",
        "OpenGVLab/InternVL3-14B",
    ],
    "image": [
        "Tongyi-MAI/Z-Image-Turbo", "Kwai-Kolors/Kolors",
        "black-forest-labs/FLUX.1-schnell", "black-forest-labs/FLUX.1-dev",
        "stabilityai/stable-diffusion-3-medium",
    ],
    "audio": [
        "FunAudioLLM/SenseVoiceSmall", "fishaudio/fish-speech-1.5",
    ],
    "embed": [
        "BAAI/bge-m3",
    ],
}


def classify(mid):
    """判断一个模型 id 属于哪一类。认不出来算 chat（语言模型最多）。"""
    s = str(mid or "").lower()
    if not s:
        return "chat"
    for h in _EMBED_HINTS:
        if h in s:
            return "embed"
    for h in _IMAGE_HINTS:
        if h in s:
            return "image"
    for h in _AUDIO_HINTS:
        if h in s:
            return "audio"
    for h in _VISION_HINTS:
        if h in s:
            return "vision"
    return "chat"


def group_models(ids):
    """把一串模型 id 按分类装进字典。embed 也返回（调用方自己决定显不显示）。"""
    out = {"chat": [], "vision": [], "image": [], "audio": [], "embed": []}
    for i in ids:
        if not i:
            continue
        out.setdefault(classify(i), []).append(str(i))
    for k in out:
        out[k].sort()
    return out


def _read_cache(base):
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("base") == base and time.time() - float(c.get("t") or 0) < CACHE_TTL:
            return c.get("ids") or []
    except Exception:
        pass
    return []


def _write_cache(base, ids):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"base": base, "t": time.time(), "ids": ids},
                      f, ensure_ascii=False)
    except Exception:
        pass


def fetch_online(base, key, timeout=12):
    """问服务商要模型列表。返回 (ids, err)。失败时 ids 为空。

    走 urllib + ProxyHandler({})：项目里所有出网请求都这么写，
    不然会被系统代理劫持，返回一个假的 502（2026-09-28 刚踩过）。
    """
    if not base or not key:
        return [], "没填地址或密钥"
    url = base.rstrip("/") + "/models"
    req = urllib.request.Request(
        url, method="GET", headers={"Authorization": "Bearer " + key})
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = op.open(req, timeout=timeout).read().decode("utf-8")
        data = json.loads(raw)
        ids = []
        for it in (data.get("data") or []):
            mid = it.get("id") if isinstance(it, dict) else None
            if mid:
                ids.append(str(mid))
        return ids, ""
    except Exception as e:
        return [], f"{type(e).__name__}: {str(e)[:120]}"


def list_models(base, key, force=False, provider="siliconflow"):
    """给 App 用的模型清单。

    返回：
        {ok, source, groups:{chat/vision/image/audio/embed}, count, err}
    source = online（刚拉到）/ cache（用的缓存）/ fallback（拉不到，用内置清单）
    """
    if not force:
        cached = _read_cache(base)
        if cached:
            return {"ok": True, "source": "cache", "groups": group_models(cached),
                    "count": len(cached), "err": ""}
    ids, err = fetch_online(base, key)
    if ids:
        _write_cache(base, ids)
        return {"ok": True, "source": "online", "groups": group_models(ids),
                "count": len(ids), "err": ""}
    if err:
        # 自定义服务商拉不到很正常（路径不一定叫 /v1/models），别报成故障
        print(f"[model_hub] 拉模型列表失败：{err}", flush=True)
    return {"ok": False, "source": "fallback", "groups": dict(FALLBACK),
            "count": sum(len(v) for v in FALLBACK.values()),
            "err": err or "拉不到模型列表"}


# ---------- 当前配置 ----------
def current(cfg):
    """从配置读出"现在用哪家、哪个模型"。key 只留前后各几位（防截图外泄）。"""
    cfg = cfg or {}
    vis = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    provider = str(cfg.get("provider") or "")
    if provider not in PROVIDERS:
        # 老配置没有 provider 字段：按 base 反推，认不出来就算自定义
        b = str(cfg.get("api_base") or "")
        provider = "siliconflow" if "siliconflow" in b else "custom"
    return {
        "provider": provider,
        "provider_name": PROVIDERS.get(provider, {}).get("name", provider),
        "api_base": str(cfg.get("api_base") or ""),
        "api_key": str(cfg.get("api_key") or ""),
        "chat": str(cfg.get("model") or ""),
        "vision": str(vis.get("model") or ""),
        "vision_enabled": bool(vis.get("enabled", True)),
        "image": str(cfg.get("image_model") or ""),
        "audio": str(cfg.get("audio_model") or ""),
        "audio_ready": False,          # 语音模块还没做，App 据此置灰
    }


def apply_cfg(cfg, p):
    """把 App 传来的设置并进配置字典（只改字典，不落盘）。

    p 里的字段名跟 current() 一致：provider / api_base / api_key /
    chat / vision / image / audio / vision_enabled。
    返回 (新配置, 改了哪些项的中文说明)。空字符串＝不改那一项。
    """
    cfg = dict(cfg or {})
    changed = []

    prov = str(p.get("provider") or "").strip()
    if prov and prov in PROVIDERS and prov != cfg.get("provider"):
        cfg["provider"] = prov
        changed.append(f"服务商 → {PROVIDERS[prov]['name']}")
        # 换服务商就该换地址，除非用户自己填了
        if not str(p.get("api_base") or "").strip() and prov != "custom":
            cfg["api_base"] = PROVIDERS[prov]["base"]
            changed.append("接口地址 → " + PROVIDERS[prov]["base"])

    base = str(p.get("api_base") or "").strip().rstrip("/")
    if base and base != cfg.get("api_base"):
        cfg["api_base"] = base
        changed.append("接口地址 → " + base)

    key = str(p.get("api_key") or "").strip()
    if key and key != cfg.get("api_key"):
        cfg["api_key"] = key
        changed.append("密钥已更新")

    chat = str(p.get("chat") or "").strip()
    if chat and chat != cfg.get("model"):
        cfg["model"] = chat
        changed.append("对话模型 → " + chat)

    vis = str(p.get("vision") or "").strip()
    vcfg = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    vcfg = dict(vcfg)
    if vis and vis != vcfg.get("model"):
        vcfg["model"] = vis
        changed.append("看图模型 → " + vis)
    if "vision_enabled" in p:
        ve = bool(p.get("vision_enabled"))
        if ve != vcfg.get("enabled", True):
            vcfg["enabled"] = ve
            changed.append("看图功能 → " + ("开" if ve else "关"))
    if vcfg:
        cfg["vision"] = vcfg

    img = str(p.get("image") or "").strip()
    if img and img != cfg.get("image_model"):
        cfg["image_model"] = img
        changed.append("生图模型 → " + img)

    aud = str(p.get("audio") or "").strip()
    if aud and aud != cfg.get("audio_model"):
        cfg["audio_model"] = aud
        changed.append("语音模型 → " + aud)

    return cfg, changed


def save(cfg):
    """写回 config.json。先把旧文件备份一份 —— 改配置改崩过不止一次。"""
    try:
        bak = CONFIG_PATH + ".bak_modelhub"
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                old = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(old)
    except Exception:
        pass
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def load():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
