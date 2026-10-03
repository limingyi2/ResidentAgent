# -*- coding: utf-8 -*-
"""语音合成（她发语音条）。

=========== 当前平台：阿里云百炼 Qwen-Audio-3.1 ===========
模型 qwen-audio-3.1-tts-flash，音色是上传参考音频复刻出来的 voice_id。

换平台有实测依据，不是照文档：两家文档都写"支持 48kHz"，但那个 48k 只是**容器**、
内容仍是窄带。用无损 WAV 逐段看频谱（mp3 编码器的噪声会污染高频指标、会骗人）：
  硅基 CosyVoice2-0.5B   8-11k -31.5dB | 11-13k -45.8dB | 有效带宽 7.7kHz
  阿里 Qwen-Audio-3.1    8-11k -13.9dB | 11-13k -31.9dB | 有效带宽 10.8kHz
8~13kHz 段高约 17dB，齿音与句尾余音的区别就落在这里；两条线 13kHz 以上都是 -90dB 空白，
所以谁都不是"真 48k"。计费按 token，每天 10 条约 ¥0.37/月。

=========== 阿里接口要点 ===========
1) 合成端点是 POST {base}/services/audio/tts/SpeechSynthesizer，**不是 OpenAI 兼容的
   /audio/speech**；body = {model, input:{text, voice, format, sample_rate}}。
2) 返回的**不是音频字节**，而是 output.audio.url 一个**临时下载地址**，要再 GET 一次。
   有效期很短，必须立刻下、绝不能存起来以后用。延迟是两段往返，合计 1.5~3s。
3) voice **直接填复刻出来的 voice_id**（qwen-audio-3.1-tts-flash-<名>-<32位hex>），
   **不能加模型名前缀**。音色与模型不匹配时阿里报的错里带 [cosyvoice:]Engine error [411]
   —— 里面写着 cosyvoice，很容易让人以为走错了端点，其实跟端点无关，纯粹是这个 voice
   不在该模型的音色列表里。
4) 复刻音色**只收公网 URL，不接受 base64**；建音色走 customization + voice-enrollment，
   只是 target_model 不同（3.1 官方文档没列，但实测可以复刻）。

=========== 旧平台（硅基流动）保留为兜底 ===========
sf_diana 是硅基的预置音色，**只在阿里侧故障时应急**。要点记下来，免得日后回去再踩：
预置音色的 voice 必须写「模型名:音色名」，克隆音色 speech:<名>:<uid>:<id> 要**原样**传
（加前缀会 400）；GET /audio/voice/list 的顶层键是 `result` 不是 `data`。
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

# --- 平台默认值 ---
DEFAULT_PROVIDER = "aliyun"

# 阿里云百炼（当前平台）
ALI_BASE = "https://dashscope.aliyuncs.com/api/v1"
ALI_MODEL = "qwen-audio-3.1-tts-flash"
ALI_FORMAT = "mp3"          # 实测 mp3/48k 一条 8 秒约 285KB；opus 只 39KB 但 App 兼容性有风险
ALI_SAMPLE_RATE = 48000

# 硅基流动（旧平台，兜底）
SF_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
SF_VOICE = "diana"

MAX_CHARS = 80          # 一条语音的文本上限，超过截断（语音条太长听着烦，也更贵）
MAX_INSTR_CHARS = 80    # 情绪指令上限。这段是给 TTS 的提示，写太长反而抓不住重点，
                        # 而且它是提示注入面 —— 收窄到一句话的长度

_VOICE_CACHE = {}

# --- 音色目录 ---
# server 的 GET /api/voices 直接读它，App 设置页渲染成列表，点一下走
# POST /api/voice/apply 热切换（以前换音色得 ssh 上云改 config 再重启）。
#
# 平台音色标识硬编码在这里而不是每次去问平台：那要联网，而设置页必须永远能打开
# —— 平台抖动时列表变空，她连自己现在是什么声音都显示不出来。
# 复刻出的 voice_id / uri 一旦生成就固定不变，硬编码不会失效（账号 id 不是密钥）。
# 以后上传了新音色往表里加一条即可：key 用英文，别改已有条目的 key。
#
# 每条必须带 provider/model：apply() 会把它们和 voice 一起写进 config，切换不再依赖
# 散落各处的默认值；硅基的 key 一律加 sf_ 前缀，避免和阿里撞名（by_key 是按 key 查的）。
VOICE_CATALOG = [
    # --- 兜底：旧平台的预置嗓，阿里全线故障时一键切回，代价是 24kHz 的合成味 ---
    {"key": "sf_diana", "label": "预置 · 欢快女声（旧平台）", "group": "兜底",
     "provider": "siliconflow", "model": SF_MODEL,
     "name": "diana", "voice": "diana",
     "desc": "硅基预置嗓，一听就是机器；只在阿里侧出问题时应急"},

    # --- 阿里 Qwen-Audio-3.1：圈定的 7 条 ---
    {"key": "zv_piper", "label": "绝区零 · 派派", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "zvpiper",
     "voice": "qwen-audio-3.1-tts-flash-zvpiper-246bc40bf08d47699b80bf7ac23f7e6b",
     "instruction": "慵懒犯困，尾音往下掉，句子之间停顿长一点",
     "desc": "慵懒、尾音往下掉，适合她犯困或撒娇"},
    {"key": "gs_xiangling", "label": "原神 · 香菱", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsxiangl",
     "voice": "qwen-audio-3.1-tts-flash-gsxiangl-a2a7b35d78884d13ac6336778a8a4c41",
     "instruction": "元气，语速偏快，语气上扬，像刚想到什么急着说",
     "desc": "亮、脆、语速偏快，元气路线"},
    {"key": "zv_cecilia", "label": "绝区零 · Cecilia", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "zvcecili",
     "voice": "qwen-audio-3.1-tts-flash-zvcecili-c94de5e639894890bcbf6723498d77c8",
     "instruction": "偏冷静，咬字清楚，音量不大，像在旁边平静地说",
     "desc": "偏冷、咬字清楚，安静说话时最好听"},
    {"key": "gs_barbara", "label": "原神 · 芭芭拉", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsbarbar",
     "voice": "qwen-audio-3.1-tts-flash-gsbarbar-0f68a5df77f449aea257986f3b632fec",
     "instruction": "甜美轻快，语气软一点，不要太用力",
     "desc": "甜美偏亮；参考音频当年做过 24kHz 无损转换"},
    {"key": "gs_ganyu", "label": "原神 · 甘雨", "group": "二游角色",
     "provider": "aliyun", "model": ALI_MODEL, "name": "gsganyu",
     "voice": "qwen-audio-3.1-tts-flash-gsganyu-7ef9ebe5858c4668bc01434ca2293754",
     "instruction": "温柔偏低，不着急，长句子读稳一点",
     "desc": "温柔偏低，长句子最稳"},

    {"key": "moning_01", "label": "莫宁", "group": "你自己录的",
     "provider": "aliyun", "model": ALI_MODEL, "name": "moning",
     "voice": "qwen-audio-3.1-tts-flash-moning-b17d12e4e1544238933058b067824d95",
     "instruction": "语气随意，像跟熟人发消息，不用刻意，语速自然",
     "desc": "人味最足，长句子也稳；2026-10-01 重录版（电平比初版好）"},
    {"key": "jiabeilina_v2", "label": "嘉贝莉娜", "group": "你自己录的",
     "provider": "aliyun", "model": ALI_MODEL, "name": "jblina",
     "voice": "qwen-audio-3.1-tts-flash-jblina-24bdbe99164b459f99d3b2da87157214",
     "instruction": "声音压低一点，语速慢半拍，语气淡淡的",
     "desc": "音色偏暗偏低，反差感强"},
]


def by_key(key):
    """按 key 找目录条目；找不到返回 None。"""
    k = str(key or "").strip()
    for it in VOICE_CATALOG:
        if it["key"] == k:
            return it
    return None


# 试听用的固定句子（App「她的声音」列表里点"试听"合成这句）。
# 选它的理由是音素覆盖够：平舌 / 翘舌、鼻音、疑问句语调都有，还有"小雨""楼门口"
# 这种容易糊的连续音节；时长 8 秒左右，正在参考音频那个区间里。
PREVIEW_TEXT = "刚刚下课，外面下了点小雨，我在楼门口等了一会儿才走。你那边天气怎么样？"


def provider_of(cfg=None):
    """当前用哪个平台。缺省 aliyun。"""
    c = (cfg or {}).get("voice") or {}
    return str(c.get("provider") or DEFAULT_PROVIDER).lower()


def current_key(cfg=None):
    """反查"现在用的是哪一条" —— App 要拿它在列表里标「· 当前」。

    按 config 里的 voice 值匹配；匹配不上（比如手工改成了目录外的音色）就把原值当 key
    返回，App 会显示成一条不认识的项，总比显示"当前=空"好。
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
    返回 (新 voice 子配置, 人话描述)；key 不在目录里就返回 (None, "")。

    **provider 和 model 必须跟着一起写**：目录里同时存在阿里和硅基两类音色，只换 voice
    值会出现「voice 是阿里的、provider 还是硅基」这种组合，synth 会拿错端点、必失败，
    所以把三者当一个整体切。

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
    # 基线情绪跟着音色一起换。不清掉旧的：切回旧音色时会留着上一条，
    # 而那条对这个音色不一定合适。硅基那条没有基线（它不吃 instruction），
    # 顺手把 key 删掉，免得留下一个接口根本不认的字段。
    instr = it.get("instruction")
    if instr:
        sub["instruction"] = instr
    else:
        sub.pop("instruction", None)
    return sub, "%s（%s）" % (it["label"], it["name"])


def ali_creds(cfg=None):
    """取阿里侧的 (base, key)。

    为什么阿里的凭据放在 `voice.aliyun` 而不是复用顶层 api_base/api_key：顶层那两个是
    **聊天模型**在用的（硅基账户），语音换平台时如果去改顶层，会顺手把聊天模型也带偏。
    """
    c = (cfg or {}).get("voice") or {}
    a = c.get("aliyun") or {}
    return (a.get("api_base") or ALI_BASE).rstrip("/"), (a.get("api_key") or "")


def verify_catalog(cfg=None, timeout=30):
    """机器核对目录里每条音色在平台侧是否真的存在。返回 (对上的key, 对不上的明细)。

    **为什么一定要有**：第一版目录里的 uri 是从平台列表的**打印输出**里复制的，而那份输出
    用 uri[:60] 截断了 —— 7 条里 5 条尾部被砍掉 1~5 个字符。字符串看着"就是那个样子"，
    眼睛根本发现不了；合成时平台只回一句 400 Voice does not exist，完全看不出是少几个
    字符。这种又长又没规律、错了还不报原因的标识串，只能机器核对。
    **改完目录跑一遍这个函数再上线。**

    两个平台的核对方式不同：硅基 GET /audio/voice/list（顶层键是 `result` 不是 `data`）；
    阿里 POST customization + `action=list_voice`（**单数**，写 list_voices 回 400）。
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

    # --- 硅基（旧平台） ---
    # 硅基的凭据在顶层 api_base/api_key（跟聊天模型共用同一份），不在 voice 段里，
    # 跟阿里正好相反，别照着阿里的写法抄
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

    阿里那条线**不走这个判断** —— 它的 voice_id 是 qwen-audio-3.1-tts-flash-<prefix>-<hex>，
    没有 speech: 前缀。
    """
    return str(voice or "").startswith("speech:")


def full_voice_name(model, voice):
    """硅基专用：把 config 里的 model/voice 拼成接口要的 voice 字符串。

    预置音色必须带「模型名:」前缀，而克隆音色本身就是完整 URI、**再加前缀会 400**
    （两种拼法不能混）。阿里那条线不用这个函数，voice_id 原样传。
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


def _cache_path(text, provider, model, voice, ext="mp3", instruction=""):
    """缓存 key 必须含 provider —— 换平台后同一句话/同一个 label 会撞上旧文件。

    也必须含 instruction：同一句话用不同情绪合成是两个不同的声音，
    不带进 key 的话第二条会直接命中第一条的缓存，听着还是没变化。
    """
    key = hashlib.md5(("%s|%s|%s|%s|%s" % (provider, text, model, voice,
                                            instruction or ""))
                      .encode("utf-8")).hexdigest()[:16]
    return os.path.join(VOICE_DIR, "v_%s.%s" % (key, ext))


def _ext_of(fmt):
    f = str(fmt or "mp3").lower()
    if f in ("wav",):
        return "wav"
    if f in ("opus", "ogg"):
        return "opus"
    return "mp3"


def synth(text, api_config, timeout=90, instruction=""):
    """合成语音，返回文件名（如 v_ab12cd34.mp3）；失败返回 ""。

    instruction 是情绪指令（"犯困、尾音往下掉"这类），直接透传给阿里的
    input.instruction。**不给它就是平读** —— TTS 拿到没有情绪指令的文本
    会照着念，像播报，这是"听着假"的最大来源。空的时候退到 config 里的
    静态 instruction（音色目录没设就是没有）。

    失败就让上层把 [voice] 标记去掉、退回发文字 —— 绝不能因为语音失败害她整条消息发不出去。
    """
    text = (text or "").strip()
    if not text or not api_config:
        return ""
    vcfg = api_config.get("voice") or {}
    if not vcfg.get("enabled", True):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
    instr = str(instruction or "").strip()[:MAX_INSTR_CHARS]

    prov = str(vcfg.get("provider") or DEFAULT_PROVIDER).lower()
    if prov == "aliyun":
        return _synth_aliyun(text, vcfg, api_config, timeout, instr)

    # --- 旧平台（硅基）：OpenAI 兼容，响应体直接就是音频字节 ---
    return _synth_siliconflow(text, api_config, vcfg, timeout)


def _synth_aliyun(text, vcfg, api_config, timeout, instruction=""):
    base, key = ali_creds(api_config)
    if not key:
        print("[语音] 阿里侧没配 key（config.voice.aliyun.api_key），跳过", flush=True)
        return ""
    model = vcfg.get("model") or ALI_MODEL
    fmt = str(vcfg.get("format") or ALI_FORMAT).lower()
    instr = str(instruction or "").strip() or str(vcfg.get("instruction") or "").strip()
    path = _cache_path(text, "aliyun", model, vcfg.get("voice") or "",
                       _ext_of(fmt), instr)
    if os.path.exists(path) and os.path.getsize(path) > 1024:
        return os.path.basename(path)     # 同一句+同一情绪不重复花钱

    os.makedirs(VOICE_DIR, exist_ok=True)
    body = {"model": model, "input": {
        "text": text,
        "voice": vcfg.get("voice"),        # voice_id 原样传，**不能加模型名前缀**
        "format": fmt,
        "sample_rate": int(vcfg.get("sample_rate") or ALI_SAMPLE_RATE)}}
    if instr:
        body["input"]["instruction"] = str(instr)[:MAX_INSTR_CHARS]
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
        # 下载地址是临时的，立刻取，别存起来复用
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
