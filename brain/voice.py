# -*- coding: utf-8 -*-
"""语音合成（她发语音条）。

=========== 当前平台：阿里云百炼 Qwen-Audio-3.1（2026-10-01 用户拍板切换）===========
模型 **qwen-audio-3.1-tts-flash**，音色是上传参考音频复刻出来的 voice_id。

**为什么从硅基流动（CosyVoice2-0.5B : diana）换过来 —— 有实测依据，不是看文档。**
文档上两家都写"支持 48kHz"，但那个 48k 只是**容器**、内容仍是窄带，所以先证伪
再比。用**无损 WAV 逐段看频谱**（mp3 编码器会把自己的噪声算进高频，会骗人）：

    硅基 CosyVoice2-0.5B   8-11k -31.5dB | 11-13k -45.8dB | 有效带宽 7.7kHz
    阿里 Qwen-Audio-3.1    8-11k -13.9dB | 11-13k -31.9dB | 有效带宽 10.8kHz

8~13kHz 段高出约 **17dB** —— 齿音（是/说/小/走）和句尾余音的区别就落在这里。
两条线 13kHz 以上都是 -90dB 的空白，所以**谁都不是"真 48k"**，但差别是实的。

计费：按 token。实测长句 66 字符 → 输入 35 tokens、输出约 110 tokens，
折合约每条 ¥0.0012（每天 10 条约 **¥0.37/月**），比硅基（¥1.48/月）还便宜约 4 倍。

=========== 阿里接口要点（2026-10-01 实测，坑都记在这）===========
1) 合成端点是 `POST {base}/services/audio/tts/SpeechSynthesizer`，
   **不是 OpenAI 兼容的 /audio/speech**；body 形如
   {"model": M, "input": {"text": T, "voice": V, "format": "mp3", "sample_rate": 48000}}
2) 返回的**不是音频字节**，而是 `output.audio.url` 一个**临时下载地址**，
   要再 GET 一次。那个 URL 有效期很短，必须立刻下、绝不能存起来以后用。
   （所以延迟是两段网络往返：合成 + 下载，实测合计 1.5~3s。）
3) `voice` 参数**直接填复刻出来的 voice_id**（形如
   `qwen-audio-3.1-tts-flash-moning-<32位hex>`），**不能加模型名前缀**。
   音色与模型不匹配时阿里回的错误里带着 `[cosyvoice:]Engine error [411]`
   —— 里面写着 cosyvoice，很容易让人以为走错了端点，其实**跟端点无关**，
   纯粹是这个 voice 不在该模型的音色列表里（第一次就栽在这，白查了一轮）。
4) 复刻音色**只收公网 URL，不接受 base64**；建音色的接口与 CosyVoice 是同一个
   `customization` + `voice-enrollment`，只是 target_model 不同
   （3.1 官方文档没列，但**实测可以复刻**，见 `.workbuddy/scripts/_ali_migrate.py`）。

=========== 旧平台（硅基流动）保留为兜底 ===========
目录里的 `sf_diana` 是硅基的预置音色，**只在阿里侧故障时应急**；provider=siliconflow
的分支保留了完整能力。硅基那套的要点也记下来，免得日后回去时再踩：
  预置音色的 voice 必须写「模型名:音色名」，克隆音色 `speech:<名>:<uid>:<id>`
  则要**原样**传（加前缀会 400）；`GET /audio/voice/list` 的顶层键是 `result` 不是 `data`。
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

# ---------------------------------------------------------------- 平台默认值
DEFAULT_PROVIDER = "aliyun"

# 阿里云百炼（当前）
ALI_BASE = "https://dashscope.aliyuncs.com/api/v1"
ALI_MODEL = "qwen-audio-3.1-tts-flash"
ALI_FORMAT = "mp3"          # 实测 mp3/48k 一条 8 秒约 285KB；opus 只 39KB 但 App 兼容性有风险
ALI_SAMPLE_RATE = 48000

# 硅基流动（旧，兜底）
SF_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
SF_VOICE = "diana"

MAX_CHARS = 80          # 一条语音的文本上限，超过截断（语音条太长听着烦，也更贵）

_VOICE_CACHE = {}

# ---------------------------------------------------------------- 音色目录
# 2026-10-01 加。起因：用户问「不能让这些在 app 快速更换吗」—— 在此之前换音色
# 得 ssh 上云改 config.json 再重启她的脑子，一个来回好几分钟。
# server 的 GET /api/voices 直接读它，App 设置页渲染成列表，点一下就走
# POST /api/voice/apply 热切换。
#
# 为什么把平台音色标识硬编码在这儿，而不是每次去问平台：
#   1) 那要联网。设置页必须**永远能打开** —— 平台抖动时列表变空、她连自己现在是
#      什么声音都显示不出来，比列表旧一点糟糕得多。
#   2) 复刻音色的 voice_id / uri 一旦生成就**固定不变**，硬编码不会失效。
#      （账号 id 不是密钥：没有 api_key 拿了也没用。）
# 以后在平台上传了新音色，往这个表里加一条即可（key 用英文，别改已有条目的 key）。
#
# 每条必须带 provider/model：
#   - apply() 会把它们和 voice 一起写进 config，**切换不再依赖散落各处的默认值**
#   - 硅基的 key 一律加 `sf_` 前缀，避免和阿里撞名（by_key 是按 key 查的）
VOICE_CATALOG = [
    # ---- 兜底：旧平台（硅基）的预置嗓。阿里全线故障时一键切回，代价是 24kHz 合成味 ----
    {"key": "sf_diana", "label": "预置 · 欢快女声（旧平台）", "group": "兜底",
     "provider": "siliconflow", "model": SF_MODEL,
     "name": "diana", "voice": "diana",
     "desc": "硅基预置嗓，一听就是机器；只在阿里侧出问题时应急"},

    # ---- 阿里 Qwen-Audio-3.1：用户 2026-10-01 圈定的 7 条 ----
    {"key": "zv_piper", "label": "绝区零 · 派派", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "zvpiper",
     "voice": "qwen-audio-3.1-tts-flash-zvpiper-246bc40bf08d47699b80bf7ac23f7e6b",
     "desc": "慵懒、尾音往下掉，适合她犯困或撒娇"},
    {"key": "gs_xiangling", "label": "原神 · 香菱", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsxiangl",
     "voice": "qwen-audio-3.1-tts-flash-gsxiangl-a2a7b35d78884d13ac6336778a8a4c41",
     "desc": "亮、脆、语速偏快，元气路线"},
    {"key": "zv_cecilia", "label": "绝区零 · Cecilia", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "zvcecili",
     "voice": "qwen-audio-3.1-tts-flash-zvcecili-c94de5e639894890bcbf6723498d77c8",
     "desc": "偏冷、咬字清楚，安静说话时最好听"},
    {"key": "gs_barbara", "label": "原神 · 芭芭拉", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsbarbar",
     "voice": "qwen-audio-3.1-tts-flash-gsbarbar-0f68a5df77f449aea257986f3b632fec",
     "desc": "甜美偏亮；参考音频当年做过 24kHz 无损转换"},
    {"key": "gs_ganyu", "label": "原神 · 甘雨", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsganyu",
     "voice": "qwen-audio-3.1-tts-flash-gsganyu-7ef9ebe5858c4668bc01434ca2293754",
     "desc": "温柔偏低，长句子最稳"},

    {"key": "moning_01", "label": "莫宁 · 你录的", "group": "你自己录的",
     "provider": "aliyun", "model": ALI_MODEL, "name": "moning",
     "voice": "qwen-audio-3.1-tts-flash-moning-b17d12e4e1544238933058b067824d95",
     "desc": "自录，人味最足；2026-10-01 重录版（48kHz，电平比初版好）"},
    {"key": "jiabeilina_v2", "label": "嘉贝莉娜 · 你录的", "group": "你自己录的",
     "provider": "aliyun", "model": ALI_MODEL, "name": "jblina",
     "voice": "qwen-audio-3.1-tts-flash-jblina-24bdbe99164b459f99d3b2da87157214",
     "desc": "自录，音色偏暗偏低，反差感强"},
]


def by_key(key):
    """按 key 找目录条目；找不到返回 None。"""
    k = str(key or "").strip()
    for it in VOICE_CATALOG:
        if it["key"] == k:
            return it
    return None


# 试听用的固定句子（App「她的声音」列表里点"试听"合成这句）。
# 选它的理由：**各种音素的覆盖要够**——有平舌/翘舌（才/走/怎）、有鼻音（们/样）、
# 有疑问句语调（怎么样），还有「小雨」「楼门口」这种容易糊的连续音节；
# 时长 8 秒左右，正好在参考音频那个区间里，能听出长句稳不稳。
# 与试听站 F:\zhixia\.workbuddy\voice_site 用的是同一句，方便两边对着听。
PREVIEW_TEXT = "刚刚下课，外面下了点小雨，我在楼门口等了一会儿才走。你那边天气怎么样？"


def provider_of(cfg=None):
    """当前用哪个平台。缺省 aliyun（2026-10-01 切换后的默认）。"""
    c = (cfg or {}).get("voice") or {}
    return str(c.get("provider") or DEFAULT_PROVIDER).lower()


def current_key(cfg=None):
    """反查「现在用的是哪一条」—— App 要拿它在列表里标「· 当前」。

    按 config 里的 voice 值匹配；匹配不上（比如手工改成了目录外的音色）
    就把原值当 key 返回，App 会显示成一条不认识的项，总比显示"当前=空"好。
    """
    c = (cfg or {}).get("voice") or {}
    v = c.get("voice") or SF_VOICE
    for it in VOICE_CATALOG:
        if it.get("voice") == v:
            return it["key"]
    return str(v)


def catalog(cfg=None):
    """给 App 设置页的清单：当前是哪个 + 分组好的可选音色。"""
    c = (cfg or {}).get("voice") or {}
    return {
        "current": current_key(cfg),
        "provider": c.get("provider") or DEFAULT_PROVIDER,
        "model": c.get("model") or (ALI_MODEL if provider_of(cfg) == "aliyun" else SF_MODEL),
        "items": [dict(it) for it in VOICE_CATALOG],
    }


def apply(cfg, key):
    """切换音色：只改传进来的字典，不落盘（落盘由调用方决定）。

    返回 (新 voice 子配置, 人话描述)。key 不在目录里就返回 (None, "")。

    **provider 和 model 必须跟着一起写**（2026-10-01 加）：
    目录里同时存在阿里和硅基两类音色，只换 voice 值的话会出现
    「voice 是阿里的、provider 还是硅基」这种组合，synth 会拿错端点、必失败。
    所以把三者当一个整体切，谁也别单独改。
    子字典是浅拷贝再改键，**aliyun 段里的 key 会原样保留**（凭据不该被切换动作弄丢）。
    """
    it = by_key(key)
    if not it:
        return None, ""
    sub = dict((cfg or {}).get("voice") or {})
    sub["enabled"] = True                 # 用户主动选声音，顺手确保开关是开的
    sub["provider"] = it.get("provider") or DEFAULT_PROVIDER
    sub["model"] = it.get("model") or ALI_MODEL
    sub["voice"] = it["voice"]
    return sub, "%s（%s）" % (it["label"], it["name"])


def ali_creds(cfg=None):
    """取阿里侧的 (base, key)。

    为什么阿里的凭据放在 `voice.aliyun` 而不是复用顶层 api_base/api_key：
    顶层那两个是**聊天模型**在用的（硅基账户）；语音换平台时如果去改顶层，
    会顺手把聊天模型也带偏。凭据跟着 voice 段走，切换平台就只动这一处。
    """
    c = (cfg or {}).get("voice") or {}
    a = c.get("aliyun") or {}
    return (a.get("api_base") or ALI_BASE).rstrip("/"), (a.get("api_key") or "")


def verify_catalog(cfg=None, timeout=30):
    """机器核对目录里每条音色在平台侧是否真的存在。返回 (对上的key, 对不上的明细)。

    **为什么一定要有（2026-10-01 血的教训，写目录的当天就踩了）**：
    第一版目录里的 uri 是从平台列表的**打印输出**里复制的，而那份输出用
    `uri[:60]` 截断了 —— 7 条里 5 条尾部被砍掉 1~5 个字符。字符串看着
    "就是那个样子"，眼睛根本发现不了；合成时平台只回一句
    `400 Voice does not exist`，完全看不出是"少几个字符"，我一度怀疑是
    账号/key/权限问题，白绕了一圈。
    **结论：这种又长又没规律、错了还不报原因的标识串，只能机器核对。**
    改完目录跑一遍这个函数再上线。

    两个平台的核对方式不同（这本身也是个坑）：
      - 硅基：GET /audio/voice/list，返回顶层键是 **`result` 不是 `data`**
      - 阿里：POST customization + `action=list_voice`（**单数**，写 list_voices 回 400）
    """
    c = (cfg or {}).get("voice") or {}
    plat = {"aliyun": None, "siliconflow": None}      # None = 没取到

    # --- 阿里 ---
    base, key = ali_creds(cfg)
    if key:
        try:
            r = httpx.post(base + "/services/audio/tts/customization",
                           headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"},
                           json={"model": "voice-enrollment",
                                 "input": {"action": "list_voice", "page_size": 100}},
                           timeout=timeout)
            out = (r.json() or {}).get("output") or {}
            plat["aliyun"] = set(str(v.get("voice_id"))
                                 for v in (out.get("voice_list") or []))
        except Exception:
            plat["aliyun"] = None

    # --- 硅基（旧平台）---
    # 硅基的凭据在**顶层** api_base/api_key（那是聊天模型在用的同一份），
    # 不在 voice 段里 —— 跟阿里正好相反，别照着阿里的写法抄。
    sf_key = c.get("api_key") or (c.get("siliconflow") or {}).get("api_key") or ""
    sf_base = (c.get("api_base") or (c.get("siliconflow") or {}).get("api_base") or "").rstrip("/")
    if sf_key and sf_base:
        try:
            r = httpx.get(sf_base + "/audio/voice/list",
                          headers={"Authorization": "Bearer " + sf_key},
                          timeout=timeout)
            data = r.json()
            items = ((data.get("result") or data.get("data") or [])
                     if isinstance(data, dict) else [])
            plat["siliconflow"] = set(str(it.get("uri") or "") for it in items)
        except Exception:
            plat["siliconflow"] = None

    def _probe(prov, ident):
        known = plat.get(prov)
        if known is None:
            return None          # 没凭据/取不到，不算失败
        return ident in known

    ok, bad = [], []
    for it in VOICE_CATALOG:
        prov = it.get("provider") or DEFAULT_PROVIDER
        if prov == "siliconflow" and not is_clone(it["voice"]):
            ok.append(it["key"])                 # 硅基预置音色没有 uri，不用核对
            continue
        hit = _probe(prov, it["voice"])
        if hit is None:
            ok.append(it["key"])                 # 无法核对，跳过（不误报）
        elif hit:
            ok.append(it["key"])
        else:
            bad.append((it["key"], it["name"], it["voice"], prov))
    return ok, bad


def is_clone(voice):
    """硅基的克隆音色（上传参考音频得到的 URI）长这样：speech:<名>:<uid>:<id>。

    注意：**阿里那条线不走这个判断** —— 它的 voice_id 是
    `qwen-audio-3.1-tts-flash-<prefix>-<hex>`，没有 speech: 前缀。
    """
    return str(voice or "").startswith("speech:")


def full_voice_name(model, voice):
    """硅基专用：把 config 里的 model/voice 拼成接口要的 voice 字符串。

    预置音色必须带「模型名:」前缀，而克隆音色本身就是完整 URI、**再加前缀会 400**
    （2026-10-01 实测，两种拼法不能混）。阿里那条线不用这个函数，voice_id 原样传。
    """
    if is_clone(voice):
        return voice
    return "%s:%s" % (model, voice)


def describe(cfg=None):
    """给设置页/日志用的一句话标注：平台 + 模型 + 音色名。"""
    c = (cfg or {}).get("voice") or {}
    prov = str(c.get("provider") or DEFAULT_PROVIDER).lower()
    model = c.get("model") or (ALI_MODEL if prov == "aliyun" else SF_MODEL)
    voice = c.get("voice") or SF_VOICE
    label = None
    for it in VOICE_CATALOG:
        if it.get("voice") == voice:
            label = it.get("label") or it.get("name")
            break
    if not label:
        # 目录外的手工音色：整串标识太长，日志/设置页都放不下，取尾部一段
        label = voice if len(voice) <= 20 else "…" + voice[-16:]
    short = "Qwen-Audio-3.1" if prov == "aliyun" else "CosyVoice2"
    return "%s · %s : %s" % (short, model, label)


def _cache_path(text, provider, model, voice, ext="mp3"):
    """缓存 key 必须含 provider —— 换平台后同一句话/同一个 label 会撞上旧文件。"""
    key = hashlib.md5(("%s|%s|%s|%s" % (provider, text, model, voice))
                      .encode("utf-8")).hexdigest()[:16]
    return os.path.join(VOICE_DIR, "v_%s.%s" % (key, ext))


def _ext_of(fmt):
    f = str(fmt or "mp3").lower()
    if f in ("wav",):
        return "wav"
    if f in ("opus", "ogg"):
        return "opus"
    return "mp3"


def synth(text, api_config, timeout=90):
    """合成语音，返回文件名（如 v_ab12cd34.mp3）；失败返回 ""。

    失败就返回空字符串 —— 上层把 [voice] 标记去掉、退回发文字。
    绝不能因为语音失败害她整条消息发不出去（2026-10-01 定）。
    """
    text = (text or "").strip()
    if not text or not api_config:
        return ""
    vcfg = api_config.get("voice") or {}
    if not vcfg.get("enabled", True):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]

    prov = str(vcfg.get("provider") or DEFAULT_PROVIDER).lower()
    if prov == "aliyun":
        return _synth_aliyun(text, vcfg, api_config, timeout)

    # ---- 旧平台（硅基）：OpenAI 兼容，直接返回二进制音频 ----
    return _synth_siliconflow(text, api_config, vcfg, timeout)


def _synth_aliyun(text, vcfg, api_config, timeout):
    base, key = ali_creds(api_config)
    if not key:
        print("[语音] 阿里侧没配 key（config.voice.aliyun.api_key），跳过", flush=True)
        return ""
    model = vcfg.get("model") or ALI_MODEL
    fmt = str(vcfg.get("format") or ALI_FORMAT).lower()
    path = _cache_path(text, "aliyun", model, vcfg.get("voice") or "", _ext_of(fmt))
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return os.path.basename(path)     # 同一句话不重复花钱

    os.makedirs(VOICE_DIR, exist_ok=True)
    body = {"model": model, "input": {
        "text": text,
        "voice": vcfg.get("voice"),        # voice_id 原样传，**不能加模型名前缀**
        "format": fmt,
        "sample_rate": int(vcfg.get("sample_rate") or ALI_SAMPLE_RATE)}}
    if vcfg.get("instruction"):
        body["input"]["instruction"] = str(vcfg["instruction"])
    try:
        r = httpx.post(base + "/services/audio/tts/SpeechSynthesizer",
                       headers={"Authorization": "Bearer " + key,
                                "Content-Type": "application/json"},
                       json=body, timeout=timeout)
        j = r.json()
        if r.status_code != 200:
            print("[语音] 阿里返回 HTTP %s：%s"
                  % (r.status_code, json.dumps(j, ensure_ascii=False)[:200]), flush=True)
            return ""
        url = ((j.get("output") or {}).get("audio") or {}).get("url")
        if not url:
            print("[语音] 阿里没给音频地址：%s"
                  % json.dumps(j, ensure_ascii=False)[:200], flush=True)
            return ""
        # 这个下载地址是临时的，立刻取，别存起来复用
        d = httpx.get(url, timeout=timeout).content
        if len(d) < 1024:
            print("[语音] 音频内容过小，疑似失败", flush=True)
            return ""
        with open(path, "wb") as f:
            f.write(d)
        u = j.get("usage") or {}
        print("[语音] 合成成功 %s（%d 字节，%s，tok %s/%s）"
              % (os.path.basename(path), len(d), describe(api_config),
                 u.get("input_tokens"), u.get("output_tokens")), flush=True)
        return os.path.basename(path)
    except Exception as e:
        print("[语音] 合成失败：%s" % str(e)[:160], flush=True)
        return ""


def _synth_siliconflow(text, api_config, vcfg, timeout):
    """旧平台分支（保留作兜底）。OpenAI 兼容接口，响应体就是音频字节。"""
    key = api_config.get("api_key")
    base = api_config.get("api_base")
    if not key or not base:
        return ""
    model = vcfg.get("model") or SF_MODEL
    voice = vcfg.get("voice") or SF_VOICE
    path = _cache_path(text, "siliconflow", model, voice, "mp3")
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return os.path.basename(path)

    os.makedirs(VOICE_DIR, exist_ok=True)
    body = {"model": model, "input": text,
            "voice": full_voice_name(model, voice),   # 预置带前缀 / 克隆原样，见 full_voice_name
            "response_format": "mp3"}
    if vcfg.get("speed"):
        body["speed"] = float(vcfg["speed"])
    try:
        r = httpx.post(base.rstrip("/") + "/audio/speech",
                       headers={"Authorization": "Bearer " + key},
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
