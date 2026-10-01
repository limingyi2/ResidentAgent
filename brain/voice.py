# -*- coding: utf-8 -*-
"""语音合成（她发语音条）。

选型与标注（用户要求把名称写清楚）
--------------------------------
模型：**CosyVoice2-0.5B**（FunAudioLLM，阿里通义，MOS 5.53，流式延迟 150ms，
中文自然度在开源里属第一梯队；硅基流动按 UTF-8 字节计费，实际一条短句不到一分钱）
音色：**diana（欢快女声）** —— 她的性格是元气女大学生；claire（温柔女声）作为备选，
     配置里改一个词就能换。可选：alex/benjamin/charles/david（男）、
     anna/bella/claire/diana（女）。
更高还原度的替代：**IndexTTS-2**（情感与音色还原度更高，$7.15/M bytes 同价），
     但当前账号的模型列表里没有它（2026-10-01 实测 /models 只返回
     CosyVoice2-0.5B 与 MOSS-TTSD-v0.5），等平台放开后在 config.json 里改
     voice.model 即可，代码不用动。

调用方式（坑：voice 参数必须写「模型名:音色名」）
------------------------------------------------
只写 'diana' 会 400 Invalid voice；`voice` 要写 "FunAudioLLM/CosyVoice2-0.5B:diana"。
第一次就是栽在这儿（2026-10-01），排查记录在 docs/设计与取舍.md。
"""
import hashlib
import json
import os

import httpx

try:
    import paths
    VOICE_DIR = os.path.join(paths.DATA_DIR, "voice")
except Exception:
    VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "voice")

DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
DEFAULT_VOICE = "diana"
MAX_CHARS = 80          # 一条语音的文本上限，超过截断（语音条太长听着烦，也更贵）

_VOICE_CACHE = {}


def describe(cfg=None):
    """给设置页/日志用的一句话标注：模型 + 音色名。"""
    c = (cfg or {}).get("voice") or {}
    model = c.get("model") or DEFAULT_MODEL
    voice = c.get("voice") or DEFAULT_VOICE
    return "%s : %s" % (model, voice)


def _cache_path(text, model, voice):
    key = hashlib.md5(("%s|%s|%s" % (text, model, voice)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(VOICE_DIR, "v_%s.mp3" % key)


def synth(text, api_config, timeout=60):
    """合成语音，返回文件名（如 v_ab12cd34.mp3）；失败返回 ""。

    失败就返回空字符串 —— 上层把 [voice] 标记去掉、退回发文字。
    绝不能因为语音失败害她整条消息发不出去（2026-10-01 定）。
    """
    text = (text or "").strip()
    if not text or not api_config or not api_config.get("api_key"):
        return ""
    vcfg = api_config.get("voice") or {}
    if not vcfg.get("enabled", True):
        return ""
    model = vcfg.get("model") or DEFAULT_MODEL
    voice = vcfg.get("voice") or DEFAULT_VOICE
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    path = _cache_path(text, model, voice)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return os.path.basename(path)     # 同一句话不重复花钱

    os.makedirs(VOICE_DIR, exist_ok=True)
    body = {"model": model, "input": text,
            "voice": model + ":" + voice,      # ← 必须带模型前缀，见模块注释
            "response_format": "mp3"}
    if vcfg.get("speed"):
        body["speed"] = float(vcfg["speed"])
    try:
        r = httpx.post(api_config["api_base"].rstrip("/") + "/audio/speech",
                       headers={"Authorization": "Bearer " + api_config["api_key"]},
                       json=body, timeout=timeout)
        r.raise_for_status()
        data = r.content
        if len(data) < 1024:
            print("[语音] 返回内容过小，疑似失败", flush=True)
            return ""
        with open(path, "wb") as f:
            f.write(data)
        print("[语音] 合成成功 %s（%d 字节，%s）"
              % (os.path.basename(path), len(data), describe(api_config)), flush=True)
        return os.path.basename(path)
    except Exception as e:
        print("[语音] 合成失败：%s" % str(e)[:160], flush=True)
        return ""


def path_of(name):
    """按文件名取路径（只允许 basename，防目录穿越）。"""
    name = os.path.basename(str(name or ""))
    p = os.path.join(VOICE_DIR, name)
    return p if os.path.exists(p) else None
