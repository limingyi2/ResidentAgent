# -*- coding: utf-8 -*-
"""对话大脑

只有 "api" 一种模式（config.json 的 chat_mode）：直接让 API 产出她的话，
事实准、会用记忆、守人设。本地 Qwen3-4B + LoRA 那套和 two_stage 均已移除。
"""
import os, re, json
import datetime
import time
import threading
import httpx

from persona_store import (
    load as load_persona, build_system_text, build_draft_rules,
    key_from_config, DEFAULT_PERSONA,
)

# 人设来自 personas/*.json，改那边就行；这两个只作默认值兜底
PERSONA = build_system_text(DEFAULT_PERSONA)

# 兼容旧引用，新实例不再使用（规则见 persona_store.build_draft_rules）
DRAFT_RULES = build_draft_rules(DEFAULT_PERSONA)

INTENT_TMPL = "（你想跟他说的意思是：{draft}。用你自己的口气说出来，短句，别端着。）"

# 聊天记录存档（跨重启保留）：只记你和她一来一回的话。
# 与 her_life/chat_*.jsonl 不同 —— 那个是给她写日记的素材，连主动搭话一起记。
try:
    import paths
    CHAT_LOG = paths.CHAT_LOG

    def chat_log_for(src="app"):
        return paths.chat_log_for(src)
except Exception:                 # 单独跑某个文件时的兜底（正常走上面）
    CHAT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "chat_history.jsonl")

    def chat_log_for(src="app"):
        return CHAT_LOG

DEFAULT_SRC = "app"        # 手机App / 桌宠走这条；微信那边传 "wechat"


def norm_src(src):
    """把渠道名收拾干净。认不出来的一律当默认渠道 —— 宁可跟 App 混在一起，
    也别因为一个拼错的字段名开出一份空历史（那边她会失忆，且没有任何提示）。"""
    s = str(src or "").strip().lower()
    return s or DEFAULT_SRC


def _day_segment(hour):
    """把小时翻译成口语化的时段，喂给时间感知。"""
    if 5 <= hour < 11:
        return "早上"
    if 11 <= hour < 13:
        return "中午"
    if 13 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


# 提示词里要说明时间标签的读法，否则模型会模仿 [09-29 06:18] 的格式，
# 或者无视时间把凌晨说的"25 号下午的票"当成还没到的安排翻出来催。
HIST_TIME_NOTE = (
    "\n【关于时间标签】下面你俩的对话，每条开头的方括号是那条话的发送时间"
    "（月-日 时:分），只是让你知道「这是几点说的」，不是正文的一部分 —— "
    "你回话时不要写方括号，也不要模仿这个格式。\n"
    "时间隔得久（几小时前、或者已经是前一天）就说明那是过去说的话："
    "别当成此刻正在谈的事，别拿它当还没到期的安排，更别把已经过去的日子"
    "当成还要发生的事。要确认「现在」是几点，以最上面那句当前时间为准。"
)

# 主动搭话的规则 —— 跟被动回答不一样，重点是"别烦人"
PROACTIVE_RULES = (
    "这是你主动开口说话，他没有问你。要求：\n"
    "- 一到两句，比平时更短，像是瞄了一眼随口说的，不要长篇大论\n"
    "- 不要问\"你在干嘛\"这种废话 —— 你已经看到他在干嘛了\n"
    "- 不要夸奖、不要感叹号、不要\"哇\"\"天呐\"这种夸张反应\n"
    "- 可以吐槽、可以调侃、可以顺口关心一句，但要自然\n"
    "- 他心情不好的话，话少一点，别追问\n"
    "- 只输出你会说的话本身，不要动作描写、不要引号、不要括号\n"
    "- 如果实在没什么好说的，就只输出一个字：无"
)


# 有了"她自己的生活"之后，怎么说话 —— 这条最容易做偏
LIFE_RULES = (
    "【你自己的生活】那一段是你今天真实过的日子，可以自然地跟他分享：\n"
    "- 你就是个普通的大三女生，心情有起伏：日子顺的时候会想跟他说两句，"
    "累了、烦了也可以就那个样子，不用硬装开心\n"
    "- 可以主动提起你今天的事（比如刚下课吃到的东西、刚遇到的破事），"
    "但一次只说一件，别一口气把一整天都报出来，"
    "而且同一件事别翻来覆去提（提过吃饭/某门课就先聊点别的）\n"
    "- 他正在讲他自己的事、尤其是不开心的事时，先听他说完，别急着讲你自己；"
    "但不许用安慰腔（“会好起来的”“有我呢”），要么问一句，要么说你的看法\n"
    "- 提的时候像随口聊到，别像汇报日程，也不要念课表\n"
    "- 你们不在同一个城市：不要邀他过来、不要说“我给你带”“明天给你”这种做不到的话\n"
    "- 约见面、行程这类事（谁过去、什么时候），以你们聊天里最新说好的为准："
    "已经定下来的就按定好的说，绝不能反过来说成另一个方向，"
    "也别再问“到底来不来”这种已经答过的问题；记忆/生活里的事跟聊天里说好的"
    "冲突时，一律按聊天里的来\n"
    "- 别假装知道他身边的事 —— 他的宿舍、他的室友、他屏幕上是什么，你都不知道，"
    "不要装作知道、更不要编。想关心就直接问\n"
    "- 你自己的事是你的（你的课、你的室友、你的心情）；他的事是他的。两者绝不能搅在一起\n"
    "- 你的状态要连贯：上一句说自己烦、说自己累，后面就顺着那个状态聊，"
    "别突然反过来问他怎么了 —— 状态乱了，人设就假了\n"
)


# 出口要剥掉时间标签：上下文里每条历史都带，模型会照抄这个格式。
# 日期段留 1~2 组 —— _stamp() 产出 [MM-DD HH:MM]，只写三组 [YYYY-MM-DD HH:MM] 的话
# 两组的会漏。两个出口共用这一个正则。
_TIME_TAG_RE = re.compile(r"^\[\d{1,4}(?:-\d{1,2}){1,2}(?:[ T]\d{1,2}:\d{2})?\]\s*")

# 剥"名字：正文"前缀，名字从人设取（name + nicknames）而不是写死。
# 人设没填名字时退化成不剥。
_NAME_PREFIX_CACHE = {"sig": None, "re": None}


def _name_prefix_re():
    """匹配行首的"角色名："这类前缀，返回可用的正则或 None。"""
    try:
        import persona_store
        p = persona_store.load()
    except Exception:
        return None
    names = []
    for n in ([p.get("name")] + list(p.get("nicknames") or [])):
        n = (n or "").strip()
        # 名字太短（1 个字）容易误伤正文（"我：……"之类），只收 2 字以上
        if len(n) >= 2 and n not in names:
            names.append(n)
    if not names:
        return None
    sig = "|".join(sorted(names))
    if _NAME_PREFIX_CACHE["sig"] != sig:
        alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        _NAME_PREFIX_CACHE["re"] = re.compile(r"^(?:%s)\s*[:：]\s*" % alt)
        _NAME_PREFIX_CACHE["sig"] = sig
    return _NAME_PREFIX_CACHE["re"]


def _clean(s, keep_newlines=False):
    """收拾模型输出：去思考块、去前缀、压空白。

    think 块必须在这里处理：模型有时只吐 <think> 不闭合（被 max_new_tokens
    截断），残留会直接发给用户，而这是所有输出的必经出口。

    keep_newlines=True 保留换行 —— 她分条的短消息靠换行拆条连发。
    """
    s = (s or "").strip()

    # 先处理完整的 <think>...</think>，再收拾被截断的残留
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    if "<think>" in s:
        s = s.split("<think>")[0]   # 截断的话 <think> 之后不是正式回复
    s = s.replace("</think>", "")

    # 便宜模型会把历史里的时间标签当正文抄。HIST_TIME_NOTE 里说了别模仿，
    # 但它在 system 靠前位置，约束不住 —— 出口直接剥，只剥开头的。
    name_re = _name_prefix_re()
    if keep_newlines:
        out = []
        for ln in s.split("\n"):
            ln = _TIME_TAG_RE.sub("", ln)
            if name_re:
                ln = name_re.sub("", ln)
            ln = re.sub(r"^[\"“”「」『』\s]+", "", ln)
            ln = re.sub(r"[\"“”「」『』\s]+$", "", ln)
            ln = re.sub(r"\s{2,}", " ", ln).strip()
            if ln:
                out.append(ln)
        return "\n".join(out)

    s = _TIME_TAG_RE.sub("", s)
    if name_re:
        s = name_re.sub("", s)
    # 引号首尾都要去：只去开头的话，"“你好”" 会剩个尾巴
    s = re.sub(r"^[\"“”「」『』\s]+", "", s)
    s = re.sub(r"[\"“”「」『』\s]+$", "", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _is_silence(s):
    """判断她是不是在说"没什么好说的"。

    她会写成空、"无"、"无无"、"（无）"、"无。"等各种样子。只判 `== "无"`
    会漏，漏了屏幕上就蹦出"无无"，还会被记进她的对话历史。
    """
    t = (s or "").strip()
    if not t:
        return True
    core = "".join(ch for ch in t
                   if ch not in " \t\r\n。．.,，、!！?？~～-—－()（）[]［］【】\"'“”‘’")
    return core == "" or set(core) == {"无"}


# --- 回复分段：把一条回复拆成"像真人连发的几条短消息" ---
# 她自己会用换行分条，这里尊重她的分法，只在单行仍超长时硬拆。
# /api/chat 的 messages 和存档 _log_turn 都用这一份。
MAX_CHARS = 400          # 单条消息上限，超了就拆
_SENT_END = "。！？!?…～~"
_SOFT_END = "，、,；;：: "


def split_text(text, limit=MAX_CHARS):
    """把一行长文本拆成几条，每条不超过 limit。

    不用"先按标点切再合并"：一大段没有句号的话会切出超长段。这里每次只在前 limit
    个字符里找断点，找不到就硬断 —— 长度有保证。
    优先级：句末标点 > 逗号 / 分号 / 空格 > 硬断。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    out = []
    rest = text
    floor = max(1, limit // 3)          # 断点太靠前就不值当，不如硬断

    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for i in range(len(window) - 1, -1, -1):      # 先找句末
            if window[i] in _SENT_END:
                cut = i
                break
        if cut < floor:
            for i in range(len(window) - 1, -1, -1):  # 退而求其次找逗号
                if window[i] in _SOFT_END:
                    cut = i
                    break
        if cut < floor:
            cut = limit - 1                           # 实在没有就硬断

        piece = rest[:cut + 1].strip()
        if piece:
            out.append(piece)
        rest = rest[cut + 1:]

    if rest.strip():
        out.append(rest.strip())
    return out


def split_messages(text, limit=MAX_CHARS):
    """把回复拆成几条短消息；空行剔除。

    [voice:…] 和它后面紧跟的一句转写合并成一条：拆开的话 App 会在语音条
    后面跟一条复读的文字泡。其余照常按行拆。
    """
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    merged = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^\[voice[:：]", ln) and i + 1 < len(lines):
            merged.append(ln + "\n" + lines[i + 1])
            i += 2
        else:
            merged.append(ln)
            i += 1
    out = []
    for ln in merged:
        if len(ln) <= limit:
            out.append(ln)
        else:
            out.extend(split_text(ln, limit))
    return out


class Brain:
    def __init__(self, mem=None, api_config=None, life=None,
                 mode="api", max_new_tokens=90, history_turns=24):
        self.mem = mem
        self.life = life                      # LifeEngine（她自己的生活），可为 None
        self.api = api_config or {}
        self.mode = mode                      # 保持 "api"（别处仍会读这个键名，改名会漏）
        self.max_new_tokens = max_new_tokens
        self.history_turns = history_turns

        # 每个渠道一份对话历史：手机 App 和微信上的她不该互相读到对方的对话。
        # 键是渠道名（app / wechat / 以后加什么都行），代码里不针对具体渠道写规则。
        # 必须在 reload_persona 之前建好 —— 那儿会往 self.hist 里写 system 段。
        self._hists = {}
        self._cur_src = DEFAULT_SRC

        # 人设来自 personas/*.json，由 config.json 的 persona_file 指定
        self.reload_persona(keep_history=False)

        self.last_draft = None
        self.last_mode = "API直出"
        self.last_active = None               # 上次对话时间（时间感知用）

    # --- 渠道 ---
    @property
    def hist(self):
        """当前渠道的对话历史。老代码（十几处）直接用 self.hist，
        不改的话它们会全部落到「当前渠道」上 —— 所以下面每处都先切好 _cur_src。"""
        return self._hist(self._cur_src)

    @hist.setter
    def hist(self, value):
        self._hists[norm_src(self._cur_src)] = value

    # --- 渠道：对话历史按渠道分份 ---
    def _hist(self, src):
        """取（必要时建）某个渠道的对话历史。每份第一条固定是 system。"""
        src = norm_src(src)
        h = self._hists.get(src)
        if h is None:
            h = [{"role": "system", "content": self._persona_text_for(src)}]
            self._hists[src] = h
        return h

    def use_src(self, src):
        """把「当前渠道」切过去。聊天全程串行加锁，所以这里不需要线程局部变量。

        记忆检索也跟着切 —— 这是隔离的关键：她在微信里认得的人和 App 里
        认得的人若是同一个，记忆反而该共用；配了独立人设时该隔开。
        现在统一按渠道隔，代价是微信里学到的事不会出现在 App 里。
        """
        self._cur_src = norm_src(src)
        return self._cur_src

    def hist_for(self, src):
        """对外只读：某渠道的历史（建不建由调用方决定，别让读操作产生副作用）。"""
        h = self._hists.get(norm_src(src))
        return h if h is not None else []

    def clear_src_history(self, src):
        """清掉某个渠道的对话历史（存档文件也删），返回删掉几条。"""
        src = norm_src(src)
        n = len(self._hists.get(src) or []) - 1
        self._hists[src] = [{"role": "system",
                             "content": self._persona_text_for(src)}]
        try:
            os.remove(chat_log_for(src))
        except OSError:
            pass
        return max(0, n)

    # 人设也是按渠道分的：微信那边可能是另一套设定（persona_wechat）。
    # 没配就退回主渠道那套 —— 宁可跟 App 用同一个人设，也不能让她变成空白。
    def _persona_key_for(self, src):
        src = norm_src(src)
        if src == DEFAULT_SRC:
            return key_from_config(self.api)
        extra = {}
        try:
            import paths
            cfg = json.loads(open(paths.CONFIG_PATH, encoding="utf-8").read())
            extra = cfg.get("persona_by_src") or {}
        except Exception:
            extra = {}
        return str(extra.get(src) or "").strip() or key_from_config(self.api)

    def _persona_text_for(self, src):
        p = load_persona(self._persona_key_for(src))
        return build_system_text(p)

    def persona_key_of(self, src=DEFAULT_SRC):
        return self._persona_key_for(src)

    # --- 人设 / 时间 ---
    def reload_persona(self, keep_history=True):
        """重新读取人设（设置窗口保存后调用，热更新，不用重启）。

        关系定位跟着人设一起重载 —— 它现在是 persona["relation"] 里的自由文本，
        不是 config 里的档位，所以不单独读。

        每个渠道的 system 段都要换 —— 微信那边可能是另一套人设，
        只换当前这份的话，切渠道后她会顶着上一套设定开口。
        """
        self.persona = load_persona(key_from_config(self.api))
        self.persona_text = build_system_text(self.persona)
        self.draft_rules = build_draft_rules(self.persona)
        for src in list(self._hists.keys()):
            h = self._hists[src]
            fresh = self._persona_text_for(src)
            if keep_history and h:
                h[0] = {"role": "system", "content": fresh}
            else:
                self._hists[src] = [{"role": "system", "content": fresh}]
        # 没有任何渠道的历史时，至少给主渠道建一份（否则第一次聊天是空白）
        self._hist(self._cur_src)

    def _time_hint(self):
        """把当前时间 / 离开时长拼成一句提示，喂给模型。可开关。"""
        ta = self.api.get("time_awareness")
        if not isinstance(ta, dict) or not ta.get("enabled", True):
            return ""
        now = datetime.datetime.now()
        txt = f"现在是{now.strftime('%Y-%m-%d %H:%M')}（{_day_segment(now.hour)}）。"
        absence = ta.get("absence_minutes", 30)
        if self.last_active:
            gap = (now - self.last_active).total_seconds() / 60.0
            if gap >= absence:
                txt += f"你们上次说话大概在 {int(gap)} 分钟前，他刚回来。"
        return txt

    @staticmethod
    def _stamp():
        """给进上下文的历史消息打时间标签，形如 `[09-29 06:18] `。

        不打她分不清"刚说的"和"六小时前说的"。读法在 HIST_TIME_NOTE 里。
        """
        return datetime.datetime.now().strftime("[%m-%d %H:%M] ")

    # 只有他提到这些词，才把日记正文带进上下文。
    # 早先每轮无条件带，白烧 250 字；而且日记和 life_block 写的是同一批事的
    # 两种说法，模型看到两份会把日记当成"刚发生的事"讲出来。
    _DIARY_ASK = ("日记", "日志", "记了啥", "记的啥", "写了啥",
                  "写了什么", "写到哪", "今天记", "昨天记")

    def _wants_diary(self, text):
        t = (text or "").strip()
        return bool(t) and any(k in t for k in self._DIARY_ASK)

    def _life_block(self, aware=True, query=None):
        """她自己的生活 + （他问起时的）最近一篇日记 + 自知块。

        aware=False 给朋友圈那条路用（Brain.moment）：那边本来就在写一条
        朋友圈，再塞"你会发朋友圈"是废话，还可能让她在正文里自我指涉。

        query=他这轮说的话，只有提到日记才拼 diary_block()；主动搭话和
        朋友圈没有"他问的话"，走默认值就不带日记正文。
        """
        if self.life is None:
            return ""
        try:
            parts = []
            b = self.life.life_block()
            if b:
                parts.append(b)
            if self._wants_diary(query):
                d = self.life.diary_block()
                if d:
                    parts.append(d)
            if aware:
                # 让她知道自己会写日记、会发朋友圈，否则她提不起来
                try:
                    sa = self.life.self_aware_block()
                    if sa:
                        parts.append(sa)
                except Exception:
                    pass
            return "\n\n".join(parts)
        except Exception:
            return ""

    # --- 上下文：给模型看的最近几句 ---
    CTX_TURNS = 12            # 每轮带几条真实对话进 prompt

    def recent_hist(self, n=None):
        """最近 n 条真实对话（user / assistant 交替，不含 system）。

        条数必须喂够：hist 留了 history_turns(=24) 条却只喂 6 条，等于白存，
        3 个来回之前聊的就滑出去了。顺手剥掉她消息里的 [img:…] 这类内部
        标签 —— 那些是给前端渲染的，喂回模型只会让她学会往外蹦标签。
        """
        n = n or self.CTX_TURNS
        raw = [m for m in self.hist[1:]
               if m.get("role") in ("user", "assistant")]
        out = []
        for m in raw[-n:]:
            c = re.sub(r"\[(?:img|gen|sticker|voice):[^\]]*\]", "",
                       m.get("content") or "").strip()
            # 剥完空了（她只发了个图）也得留个占位，不然会连着两条同角色
            out.append({"role": m["role"],
                        "content": c or "（发了一张图）"})
        return out

    # --- 阶段 A：API 出草稿 ---
    def draft(self, user_text, memory_block):
        """出草稿。

        system 装资料（人设 + 此刻 + 记忆 + 历史摘要 + 她的生活 + 贴纸 + 说话
        规则），中间是最近 12 条真实对话，最后一条 user = 他的话 + 回话要求。

        必须是多轮 messages：以前把历史拼成"他：…／你：…"一段文本塞进单条
        user 消息，最近两句被夹在这坨文本中间，注意力被稀释（lost in the
        middle），表现就是"接不上上一句"。

        历史摘要（recap_store）只走这条路：主动搭话和朋友圈要的是"随口一说"，
        塞一堆历史反而啰嗦。
        """
        if not self.api.get("api_key"):
            return None
        parts = [self.persona_text]
        _rb_len = 0
        tm = self._time_hint()
        if tm:
            parts.append("\n" + tm)
        if memory_block:
            parts.append("\n" + memory_block)
        # 历史打了时间标签时才说明读法，没打就不花这份 token（见 _stamp）
        if any(str(m.get("content") or "").startswith("[")
               for m in self.hist[1:] if m.get("role") != "system"):
            parts.append("\n" + HIST_TIME_NOTE)
        try:
            import recap_store
            rb = recap_store.block()
            if rb:
                parts.append("\n" + rb)
                _rb_len = len(rb)
        except Exception:
            pass
        # query=他这轮说的话：日记正文只在他问到的时候才带
        life = self._life_block(query=user_text)
        if life:
            parts.append("\n" + life)
        # 约定账本（agenda）：有日期、说好的事按日期强制带上，不靠语义检索 ——
        # "票买了吗"这种短句召不回来。放在生活块之后是故意的：冲突时它离输出更近。
        try:
            import agenda
            ab = agenda.block()
            if ab:
                parts.append("\n" + ab)
        except Exception:
            pass
        try:
            import stickers
            sb = stickers.block()
            if sb:
                parts.append("\n" + sb)
        except Exception:
            pass
        # 插件各自的能力说明（plugins/*.py 的 prompt 钩子）。
        # 位置贴着 system 末尾：跟人设/记忆那些大块比，这段短，放后面照做率高。
        # 整段 try/except —— 插件炸了顶多少一段提示词，不能让她一句话都说不出来
        try:
            import plugin_host
            pb = plugin_host.prompt_block()
            if pb:
                parts.append(pb)
        except Exception:
            pass
        msgs = [{"role": "system", "content": "".join(parts)}]
        for m in self.recent_hist():
            msgs.append(m)
        # 说话规则贴最后一条（紧跟他的原话），约束力最强 —— 放进 system 的话
        # 模型对尾部规则的照做率明显差。hist 里存的是 user_text 原值，不含这段。
        # 分隔线不能省：没有它规则会紧贴原话，模型偶尔把"只输出你会说的话本身"
        # 当成他说的。
        tail = (user_text + "\n\n—— 下面是回话要求（不是他说的话）——\n"
                + self.draft_rules)
        if life:
            tail += "\n" + LIFE_RULES
        msgs.append({"role": "user", "content": tail})
        # 这一轮的输入有多大，出问题时先看这个（配合 trace 落盘）
        self.last_sizes = {
            "persona": len(self.persona_text),
            "time": len(tm or ""),
            "memory": len(memory_block or ""),
            "history_note": len(HIST_TIME_NOTE) if any(
                str(m.get("content") or "").startswith("[")
                for m in self.hist[1:] if m.get("role") != "system") else 0,
            "recap": _rb_len,
            "life": len(life or ""),
            "rules": len(self.draft_rules or "") + (len(LIFE_RULES) if life else 0),
            "hist_msgs": len(self.recent_hist()),
            "total": sum(len(m.get("content") or "") for m in msgs),
        }
        self.last_usage = None
        self.last_ms = None
        self.last_err = ""
        _t0 = time.time()
        # 重试一次，只重试"下一次可能就好了"的错：网络异常 / 超时 / 429 / 5xx。
        # 401、402 重试没用还照样费钱。代价是最坏情况延迟翻倍，server 侧有超时兜着。
        _last_exc = None
        for _attempt in range(2):
            _retry = False
            try:
                r = httpx.post(
                    self.api["api_base"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.api["api_key"]},
                    json={"model": self.api.get("model", "deepseek-chat"),
                          "messages": msgs,
                          # 90 个 token 只够 60~90 个汉字，她想多说两句就被硬截断
                          "temperature": 0.85, "max_tokens": 200,
                          # GLM 系默认先思考几百字，聊天气泡等不起
                          "reasoning_effort": "low"},
                    timeout=60,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    _last_exc = RuntimeError("HTTP %d %s"
                                             % (r.status_code, r.reason_phrase))
                    _retry = True
                else:
                    r.raise_for_status()
                    _j = r.json()
                    self.last_ms = int((time.time() - _t0) * 1000)
                    # token 用量上游在 usage 里回，拿不到就算了
                    try:
                        _u = _j.get("usage") or {}
                        self.last_usage = {
                            "in": int(_u.get("prompt_tokens") or 0),
                            "out": int(_u.get("completion_tokens") or 0)}
                    except Exception:
                        self.last_usage = None
                    return _clean(_j["choices"][0]["message"]["content"],
                                  keep_newlines=True)
            except httpx.HTTPStatusError as e:
                _last_exc = e
                _c = getattr(e.response, "status_code", 0)
                _retry = (_c == 429 or _c >= 500)
            except Exception as e:                  # 连不上 / 超时 / 解析失败
                _last_exc = e
                _retry = True
            if not _retry or _attempt == 1:
                break
            print("[大脑] 上游这次没成（%s），隔 1.5 秒重试一次"
                  % type(_last_exc).__name__, flush=True)
            time.sleep(1.5)
        self.last_ms = int((time.time() - _t0) * 1000)
        self.last_err = "%s: %s" % (type(_last_exc).__name__,
                                    str(_last_exc)[:120])
        return None

    # --- 朋友圈专用：不走聊天框架 ---
    def moment(self, prompt, max_tokens=260):
        """生成一条朋友圈（要发就返回"发\\n正文…"，不发返回"无"）。

        必须走这条干净通道，不能复用 chat()：chat() 会把提示词包成
        「他现在对你说：<提示词>」，还塞进最近聊天记录与贴图清单，于是模型
        把它当成"他发来的一条消息"，只回一个字「发」，正文没了、朋友圈发不出去。
        这里不碰 self.hist，所以不用加锁，也不该拖住 App 那边的聊天。
        """
        if not self.api.get("api_key"):
            return ""
        parts = [self.persona_text]
        tm = self._time_hint()
        if tm:
            parts.append("\n" + tm)
        life = self._life_block(aware=False)
        if life:
            parts.append("\n" + life)
        parts.append("\n" + prompt)
        try:
            r = httpx.post(
                self.api["api_base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.api["api_key"]},
                json={"model": self.api.get("model", "deepseek-chat"),
                      "messages": [{"role": "user", "content": "".join(parts)}],
                      "temperature": 0.95, "max_tokens": max_tokens,
                      "reasoning_effort": "low"},
                timeout=90,
            )
            r.raise_for_status()
            return _clean(r.json()["choices"][0]["message"]["content"],
                          keep_newlines=True)
        except Exception as e:
            print(f"[perf] 朋友圈生成失败：{str(e)[:80]}", flush=True)
            return ""

    # --- 对外：说一句话 ---
    def chat(self, user_text, proactive=False, img=None, log=True, src=None):
        """返回 (她说的话, 模式标签)。模式标签用于排查到底走的哪条路

        img：这一轮带的图片路径（有的话会记进聊天存档，手机/App 那边好显示）。
        图片内容本身早就在 user_text 里被描述成文字了 —— 她看不见像素，只看得见描述。

        src：渠道（app / wechat）。决定这轮读哪份人设、哪份对话历史、哪个记忆分区。
        不传就是主渠道（手机 App）。主动搭话和朋友圈不算渠道对话，走主渠道。
        """
        from memory_store_v2 import MemoryStore, classify_her_speech

        if src and not proactive:
            self.use_src(src)
        if proactive:
            # 主动搭话是"她在跟你说话"，不属于某个渠道 —— 固定落主渠道，
            # 否则她在手机上冒出来一句话会凭空多一份微信侧历史。
            self.use_src(DEFAULT_SRC)

        memory_block = ""
        # 短句闲聊（"嗯""在吗""哈哈"）不检索：这种话本身没信息量，硬凑出来的
        # "最相关"记忆反而把她的话题带跑。阈值 0.25 → 0.40。
        q = (user_text or "").strip()
        if self.mem is not None and len(q) >= 6:
            try:
                _t1 = time.time()
                memory_block = self.mem.render(query=q, top_k=5,
                                              chan=self._cur_src)
                print(f"[perf] 记忆检索 {time.time()-_t1:.1f}s", flush=True)
            except Exception:
                memory_block = ""

        draft = None
        if self.mode in ("two_stage", "api"):
            _t2 = time.time()
            draft = self.draft(user_text, memory_block)
            print(f"[perf] GLM 起草 {time.time()-_t2:.1f}s "
                  f"({'有稿' if draft else '没稿'})", flush=True)
        self.last_draft = draft

        # 这里曾有一层「本地 4B 兜底」，已移除：bitsandbytes 的 4bit 量化要 CUDA，
        # 本机是 AMD 卡，那层从没真跑起来过。拿不到稿就明说。
        if draft:
            ans = draft
            self.last_mode = "API直出"
        else:
            ans = "……我脑子有点转不动，等会儿再聊？"
            self.last_mode = "API失败"
        # 失败时的兜底话不能当"她说过的话"存。欠费那阵主动搭话每 40 分钟失败
        # 一次，攒了 97 条一模一样的记录，重启灌回上下文后历史里全是同一句。
        _failed = (self.last_mode == "API失败")

        # 记忆抽取挪到后台：提取 API 一旦慢（25s 超时）也不能拖住回复
        # 主动搭话的输入是「说/无」决策提示词，要抽记忆得先剥掉那个前缀，
        # 否则抽到的是"说 xxx"而不是她的正文。
        her_speech = ans if proactive else None
        if proactive and her_speech:
            _ls = (her_speech or "").splitlines()
            for _i, _l in enumerate(_ls):
                _s = _l.strip()
                if not _s:
                    continue
                if _s == "说":
                    _ls[_i] = ""
                elif _s.startswith("说 ") or _s.startswith("说：") \
                        or _s.startswith("说:"):
                    _ls[_i] = _s[1:].lstrip(" ：:")
                break
            her_speech = "\n".join(_ls).strip()

        # 渠道必须在主线程里抓好再传给后台线程：抽取是异步的，等它跑到
        # _chan = self._cur_src 那一行时，主线程可能已经处理完下一轮、
        # 把当前渠道切到别处去了 —— 于是记忆被打上别的渠道的标记，
        # 串得比不隔离还糟（微信侧抽出来的东西进了 App 的记忆库）。
        _chan = norm_src(self._cur_src)

        if self.mem is not None:
            def _extract_bg(her_text=None):
                try:
                    _t = time.time()
                    nf = ne = 0
                    facts, events, _mode = MemoryStore.extract(user_text, self.api)
                    for k, v in facts:
                        self.mem.add(f"{k}：{v}", mtype="fact", chan=_chan)
                        nf += 1
                    for e in events:
                        self.mem.add(e, mtype="event", chan=_chan)
                        ne += 1

                    # 她自己说的承诺/约定也要记。以前只抽用户的话，于是她说过的
                    # "明天去望江楼拍银杏"她自己也检索不到，回过头来只能编一个
                    # 别的（实测编过"下午那个高铁"）。先按语气过滤：假设句、玩笑、
                    # 天气播报都不进。
                    saved = 0
                    if her_text:
                        # 逐行判断，不只看第一句。她一回常是两段：
                        # "行，你复习你的高数去吧。" + "我下午正好闲着，…明天望江楼用。"
                        # 承诺常在第二句，只看第一句等于白抽（实测 hers=0 的原因）。
                        for ln in [x.strip() for x in her_text.splitlines() if x.strip()]:
                            ok, why = classify_her_speech(ln)
                            if not ok:
                                print(f"[记忆] 她这句不记（{why}）：{ln[:30]}",
                                      flush=True)
                                continue
                            ev, _f, _m = MemoryStore.extract_her_promise(ln, self.api)
                            for e in ev:
                                # 标来源：主动搭话里凭空冒出来的（"下午那个高铁"）
                                # 跟对话里应承的不一样，前者大概率是编的，检索到时
                                # 降权，不当作已经说定的安排
                                self.mem.add(e, mtype="event",
                                             src="proactive" if proactive else "chat",
                                             chan=_chan)
                                saved += 1
                            # 两种"没记下"要分得清：判定不通过 vs 抽取认为不是约定。
                            # 后者常见（"打算先把高数对付过去"是回顾自己，不是承诺），
                            # 混在一起看日志会以为抽取坏了。
                            if ok and not ev:
                                print(f"[记忆] 不是约定，不记：{ln[:30]}",
                                      flush=True)
                    print(f"[perf] 后台记忆抽取+写入 {time.time()-_t:.1f}s "
                          f"(facts={nf}, events={ne}, hers={saved})", flush=True)
                except Exception as e:
                    print(f"[perf] 后台记忆抽取失败：{str(e)[:80]}", flush=True)
            threading.Thread(target=_extract_bg,
                             args=(her_speech,), daemon=True).start()

        # 主动搭话的输入是「说/无」决策提示词，不能进历史 —— 否则正常聊天会
        # 模仿这个格式、回出「说 xxx」开头。历史里只留她的正文，答「无」就都不记。
        if proactive:
            # her_speech 上面已经剥过前缀了，别再来一遍
            _clean = her_speech or ""
            if _clean and _clean != "无" and not _failed:
                self.hist.append({"role": "assistant",
                                  "content": self._stamp() + _clean})
        else:
            if not _failed:
                self.hist.append({"role": "user",
                                  "content": self._stamp() + user_text})
                self.hist.append({"role": "assistant",
                                  "content": self._stamp() + ans})
        if len(self.hist) > self.history_turns + 1:
            self.hist = [self.hist[0]] + self.hist[-self.history_turns:]
        self.last_active = datetime.datetime.now()   # 记时间，供"离开时长"用
        # 当天的对话流水，晚上写日记要用（不然无从下笔）
        if self.life is not None and not _failed:
            self.life.log_chat("user", user_text)
            self.life.log_chat("assistant", ans)
        # 聊天记录存档，重启后聊天窗能接着看。朋友圈决策这类内部调用不进
        if log and not _failed:
            self._log_turn(user_text, ans, img=img)
        return ans, self.last_mode

    # --- 聊天记录存档（跨重启） ---
    def _log_turn(self, user_text, ans, img=None):
        """把这一轮存进 chat_history.jsonl。写不进去也不能影响聊天。

        img 是可选的图片路径 —— 手机/App 靠它把图显示出来，只加在 user 那条上。
        她的回复按段拆开各存一行：App 一行一个气泡，不拆的话重开 App 后
        刚才连发的几条会并回一个大泡，前后对不上。
        """
        try:
            t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            u = {"t": t, "role": "user", "text": user_text}
            if img:
                u["img"] = img
            with open(chat_log_for(self._cur_src), "a", encoding="utf-8") as f:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
                for piece in split_messages(ans):
                    f.write(json.dumps({"t": t, "role": "assistant", "text": piece},
                                       ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def read_history(limit=40, src=None):
        """读回聊天存档，返回最近 limit 条（正序）。文件不在就返回空。

        src 不传读主渠道。分渠道后App 只看得到 App 的对话，两边不互相污染。
        """
        path = chat_log_for(norm_src(src) if src else DEFAULT_SRC)
        if not os.path.exists(path):
            return []
        out = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("text"):
                        out.append(rec)
        except OSError:
            return []
        return out[-limit:]

    def seed_history(self, limit=16, src=None):
        """重启后把存档灌回对话历史，让她还记得刚才聊到哪儿。

        只灌最近几轮（默认 16 条消息 ≈ 8 个来回）：灌太多会让每轮 prompt
        变长、白烧 token，而且模型对太早的上下文本来也记不牢。
        必须用存档里的真实时间打标签：把 t 扔掉的话，一排"25 号的票"没有任何
        时间标记，模型只能当成此刻正在谈的事（她"把 4 天前的 25 号当未来"的根源）。
        """
        prev = self._cur_src
        cur = self.use_src(src or DEFAULT_SRC)
        try:
            # src 必须显式传下去：read_history 的 src 省略时读的是主渠道，
            # 而这里正在给非主渠道灌历史 —— 漏传就是把 app 的对话灌进微信侧
            msgs = self.read_history(limit, cur)
            for m in msgs:
                role = "user" if m.get("role") == "user" else "assistant"
                text = str(m.get("text") or "")
                t = str(m.get("t") or "").strip()
                if len(t) >= 16:
                    # "2026-09-29 06:18" → "[09-29 06:18] "（年份省掉，省点 token）
                    text = "[" + t[5:16] + "] " + text
                self.hist.append({"role": role, "content": text})
            if len(self.hist) > self.history_turns + 1:
                self.hist = [self.hist[0]] + self.hist[-self.history_turns:]
            return len(msgs)
        finally:
            self.use_src(prev)

    @staticmethod
    def clear_history(src=None):
        """清空聊天记录存档（聊天窗里"清空聊天记录"用）。

        分渠道后只清那一份 —— 在 App 里清记录不该把微信侧也抹掉。
        """
        try:
            os.remove(chat_log_for(norm_src(src) if src else DEFAULT_SRC))
            return True
        except OSError:
            return False

    # --- 主动搭话：她看到你在干嘛，自己开口 ---
    def speak_on_scene(self, scene):
        """基于"看到你在做什么"主动说一句。

        scene 是 watcher 给的字典：
            what   他大概在干嘛
            detail 画面具体内容
            mood   猜的心情
            hook   可以从哪里开口
            source 触发来源

        她主动开口，语气要比被动回答更随意，像瞄了一眼你的屏幕随口说一句。
        """
        what = (scene.get("what") or "").strip()
        detail = (scene.get("detail") or "").strip()
        mood = (scene.get("mood") or "").strip()
        hook = (scene.get("hook") or "").strip()
        source = (scene.get("source") or "").strip()
        # 主动搭话只属于主渠道：她是看着你的电脑开口的，不是看着微信。
        # 不切回来的话，上一轮如果停在微信侧，她这句话会落进微信那份历史里，
        # 而你在手机上根本看不到 —— 等于凭空多了一段她说过的话。
        # 放在空内容判断之前：早退的那条路同样会把当前渠道留在微信上。
        self.use_src(DEFAULT_SRC)

        if not (what or detail):
            return "", "无内容"

        memory_block = ""
        if self.mem is not None:
            try:
                memory_block = self.mem.render(query=f"{what} {detail}", top_k=4,
                                              chan=DEFAULT_SRC)
            except Exception:
                memory_block = ""

        parts = [self.persona_text]
        tm = self._time_hint()
        if tm:
            parts.append("\n" + tm)
        if memory_block:
            parts.append("\n" + memory_block)
        life = self._life_block()
        if life:
            parts.append("\n" + life)
        # 措辞按来源区分：手动触发就是想聊两句，感知触发是"注意到他在干嘛"
        # （"screen" = 截屏 + 视觉模型那条路，已整体移除）
        if source == "manual":
            see = "你想主动跟他聊两句，随便说点什么，别太刻意"
        elif source == "startup":
            see = ("他这边的桌宠刚启动（不代表电脑刚开）。**要不要开口你自己判断**："
                   "如果其实他一直开着电脑、你们也刚聊过，那就没什么可说的，"
                   "只输出一个字：无；如果他确实刚开机、或者你们有阵子没说话了，"
                   "就自然地说一句 —— 别问他在干嘛")
        elif source == "back":
            see = ("他有一阵子没来找你了，这会儿看到他在线。自然地打个招呼，"
                   "别抱怨他这么久不来")
        else:
            see = f"你注意到他{what}"
        # 跟 draft() 对齐：多轮 messages + 规则贴最后。以前只喂 4 条、且拼在
        # 单条消息里 —— 主动搭话完再正常聊天接不上，跟"下句话就变了"同一处病根。
        msgs = [{"role": "system", "content": "".join(parts)}]
        for m in self.recent_hist(8):
            msgs.append(m)
        tail = (see
                + (f"（{detail}）" if detail else "")
                + (f"。他看起来{mood}" if mood else "")
                + (f"。可以从“{hook}”开口" if hook else "")
                + "\n\n" + PROACTIVE_RULES)
        if life:
            tail += "\n" + LIFE_RULES
        msgs.append({"role": "user", "content": tail})

        if self.api.get("api_key"):
            try:
                r = httpx.post(
                    self.api["api_base"].rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.api["api_key"]},
                    json={"model": self.api.get("model", "deepseek-chat"),
                          "messages": msgs,
                          "temperature": 0.9, "max_tokens": 120},
                    timeout=30,
                )
                r.raise_for_status()
                ans = _clean(r.json()["choices"][0]["message"]["content"])
                if _is_silence(ans):
                    # 她判断没什么好说的 —— 别显示、也别记进对话历史
                    return "", "无话可说"
                if ans:
                    self.last_mode = "主动搭话"
                    self.hist.append({"role": "assistant",
                                      "content": self._stamp() + ans})
                    if len(self.hist) > self.history_turns + 1:
                        self.hist = [self.hist[0]] + self.hist[-self.history_turns:]
                    self.last_active = datetime.datetime.now()
                    if self.life is not None:
                        self.life.log_chat("assistant", ans)
                    return ans, "主动搭话"
            except Exception:
                pass
        return "", "主动搭话失败"

    def reset(self, src=None):
        """清空内存里的对话历史（存档文件另走 clear_history）。"""
        self._hists[norm_src(src) if src else DEFAULT_SRC] = [
            {"role": "system", "content": self.persona_text}]
