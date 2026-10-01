# -*- coding: utf-8 -*-
"""角色 · 对话大脑

只有一种模式（config.json 的 chat_mode）
---------------------------------------
"api"（默认）  API 直接产出她的话。实测最准、最自然：
              事实对、会用记忆、有人设、不编造。

本地 Qwen3-4B + LoRA 那一整套已于 2026-09-21 移除：models/ 里 13GB 权重、
pet.py 的 ModelLoader、本文件的 speak() 一并删掉。删它的理由写在 chat() 里。

（历史遗留：早期试过 "two_stage" = API 出草稿 -> 本地 4B 改写。
 本地模型会顺着自己的习惯把草稿改坏，早就弃用了。）
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

# 人设不再写死在代码里 —— 改 personas/*.json 就行，不用动这里。
# 模块级 PERSONA 保留（= 默认人设文本），但 Brain 实例会用 config.json 指定的那套。
PERSONA = build_system_text(DEFAULT_PERSONA)

# 兼容旧引用（不再用于新实例；人设相关的规则见 persona_store.build_draft_rules）
DRAFT_RULES = build_draft_rules(DEFAULT_PERSONA)

INTENT_TMPL = "（你想跟他说的意思是：{draft}。用你自己的口气说出来，短句，别端着。）"

# 聊天记录存档（跨重启保留）。
# 跟 her_life/chat_*.jsonl 不是一回事：那个是给她写日记用的素材，连主动搭话一起记；
# 这个是"聊天窗里的聊天记录"，只记你和她一来一回的话，重启后能接着看、她也记得聊到哪。
try:
    import paths
    CHAT_LOG = paths.CHAT_LOG
except Exception:                 # 单独跑某个文件时的兜底（正常走上面）
    CHAT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "chat_history.jsonl")


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


# 对话记录里每条开头的方括号是时间标签（见 Brain._stamp / seed_history）。
# 不说明的话模型会犯两种错（2026-09-29 都实测到了）：
#   1) 把标签当正文一起模仿，回话时开头也写个 [09-29 06:18]；
#   2) 直接忽略它，把凌晨说的"约定的那个周末"当成此刻正在谈的事翻出来催。
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
    "而且同一件事别翻来覆去提（提过图书馆/某门课就先聊点别的）\n"
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


def _clean(s, keep_newlines=False):
    """收拾模型输出：去思考块、去前缀、压空白

    think 块的处理原先放在本地生成那段（2026-09-21 随本地模型一起删掉了），
    那段只有 rsplit("</think>") 一句，能应付"闭合了"的情况，
    但模型有时候只吐 <think> 没有闭合（生成被 max_new_tokens 截断），
    这时候那个残留会直接发给用户。_clean 是所有输出的必经之路，
    放这儿两条路都覆盖。

    keep_newlines=True：保留换行。她自己分条的短消息（一行一条）靠
    换行传给 QQ 端拆条连发 —— 2026-09-20 之前这里把换行洗成空格，
    结果模型分好了条也被揉成一大段，QQ 只能整段发。
    """
    s = (s or "").strip()

    # 思考块：先处理完整的 <think>...</think>，再收拾没闭合的残留
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    # 没闭合的（被截断）：<think> 之后的内容全丢，因为那不是正式回复
    if "<think>" in s:
        s = s.split("<think>")[0]
    s = s.replace("</think>", "")

    # 时间标签前缀：上下文里每条历史都带 `[09-29 06:18] `（见 Brain._stamp），
    # 便宜模型会把它当正文模仿，开头也写个方括号时间戳（2026-10-01 实测：
    # "[10-01 12:38] 那就现学呗…"）。HIST_TIME_NOTE 里明写了不要模仿，但它在
    # 提示词中段、注意力不够 —— 与其指望模型守规矩，不如在所有输出的必经
    # 出口直接剥掉。只剥开头的（正文里她提到时间是另一回事）。
    if keep_newlines:
        out = []
        for ln in s.split("\n"):
            ln = re.sub(r"^\[\d{1,4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2})?\]\s*", "", ln)
            ln = re.sub(r"^(角色|角色|角色|角色)\s*[:：]\s*", "", ln)
            ln = re.sub(r"^[\"“”「」『』\s]+", "", ln)
            ln = re.sub(r"[\"“”「」『』\s]+$", "", ln)
            ln = re.sub(r"\s{2,}", " ", ln).strip()
            if ln:
                out.append(ln)
        return "\n".join(out)

    s = re.sub(r"^\[\d{1,4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2})?\]\s*", "", s)
    s = re.sub(r"^(角色|角色|角色|角色)\s*[:：]\s*", "", s)
    # 引号：开头和结尾都要去（之前只去了开头，"“你好”" 会剩个尾巴）
    s = re.sub(r"^[\"“”「」『』\s]+", "", s)
    s = re.sub(r"[\"“”「」『』\s]+$", "", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def _is_silence(s):
    """判断她是不是在说"没什么好说的"。

    她会写成各种样子：空、"无"、"无无"、"（无）"、"无。"。
    以前只判 `== "无"`，实测她写了"无无"，结果屏幕上蹦出两个字"无无"，
    而且这俩字还会被记进她的对话历史 —— 所以她得在这儿被拦下来。
    """
    t = (s or "").strip()
    if not t:
        return True
    core = "".join(ch for ch in t
                   if ch not in " \t\r\n。．.,，、!！?？~～-—－()（）[]［］【】\"'“”‘’")
    return core == "" or set(core) == {"无"}


class Brain:
    def __init__(self, mem=None, api_config=None, life=None,
                 mode="api", max_new_tokens=90, history_turns=24):
        self.mem = mem
        self.life = life                      # LifeEngine（她自己的生活），可为 None
        self.api = api_config or {}
        self.mode = mode                      # 保持 "api"（历史遗留分支，别处仍会读）
        self.max_new_tokens = max_new_tokens
        self.history_turns = history_turns

        # 人设从 personas/*.json 读（由 config.json 的 persona_file 指定）
        self.reload_persona(keep_history=False)

        self.last_draft = None
        self.last_mode = "API直出"
        self.last_active = None               # 上次对话时间（时间感知用）

    # ---------- 人设 / 时间 ----------
    def reload_persona(self, keep_history=True):
        """重新读取人设（设置窗口保存后调用，热更新，不用重启）。"""
        self.persona = load_persona(key_from_config(self.api))
        self.persona_text = build_system_text(self.persona)
        self.draft_rules = build_draft_rules(self.persona)
        if keep_history and getattr(self, "hist", None):
            self.hist[0] = {"role": "system", "content": self.persona_text}
        else:
            self.hist = [{"role": "system", "content": self.persona_text}]

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

        不打标签时模型分不清"刚说的"和"六小时前说的" —— 实测她会在上午
        把凌晨那句"约定的那个周末"当成此刻正在谈的事反复提，因为上下文里
        一排消息全是"约定的那个周末"，没有任何一个标记告诉它那是几小时前的。
        标签的读法在 HIST_TIME_NOTE 里向她说明（避免她把格式也模仿了）。
        """
        return datetime.datetime.now().strftime("[%m-%d %H:%M] ")

    # 只有他提到这些词，才把日记正文带进上下文（2026-09-29 用户拍板）。
    #
    # 以前是**每轮无条件带**，跟他说什么毫无关系，三个坏处：
    #   1) 每轮白烧约 250 字；
    #   2) 日记是流水账，跟 life_block 里"你今天自己经历的事"写的是同一批事
    #      的两种说法，模型看到两份，容易把日记里的内容当成"刚发生的事"讲出来
    #      （说话规则里"刚说过的日常不要再复述一遍"就是为它打的补丁）；
    #   3) 今天的日记一写完（22:30 后）diary_block() 反而返回空 —— 最该有
    #      内容的那一刻恰好没有，行为跟着时段跳。
    # 现在改成按需：他问起才带，正好是唯一需要正文的时机。
    _DIARY_ASK = ("日记", "日志", "记了啥", "记的啥", "写了啥",
                  "写了什么", "写到哪", "今天记", "昨天记")

    def _wants_diary(self, text):
        t = (text or "").strip()
        return bool(t) and any(k in t for k in self._DIARY_ASK)

    def _life_block(self, aware=True, query=None):
        """她自己的生活 + （他问起时的）最近一篇日记 + 自知块。

        aware=False 给生朋友圈那条路用（Brain.moment）：那边本来就在写一条朋友圈，
        再塞"你会发朋友圈"进去是废话，还可能让她在正文里自我指涉。

        query=他这轮说的话。只有提到日记才拼 diary_block()；主动搭话和
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
                # 2026-09-29：让她知道自己会写日记、会发朋友圈。
                # 以前只有 diary_block 会告诉她"你最近那篇日记写了啥"，
                # "这两件事是你自己做的"从来没说过 —— 所以她提不起来。
                try:
                    sa = self.life.self_aware_block()
                    if sa:
                        parts.append(sa)
                except Exception:
                    pass
            return "\n\n".join(parts)
        except Exception:
            return ""

    # ---------- 上下文：给模型看的最近几句 ----------
    CTX_TURNS = 12            # 每轮带几条真实对话进 prompt（2026-09-29：6 → 12）

    def recent_hist(self, n=None):
        """最近 n 条真实对话（user / assistant 交替，不含 system）。

        2026-09-29 用户反馈"说完这句下句就变了"。查下来的第一个原因：
        `hist` 明明留了 history_turns(=24) 条，draft() 却只喂最后 6 条
        —— 等于白存。3 个来回一过，前面聊的就滑出去了。

        顺手剥掉她消息里的 [img:名字] / [gen:...] 这类内部标签：
        那些是给前端渲染用的，喂回模型只会让她学会往外蹦标签。
        """
        n = n or self.CTX_TURNS
        raw = [m for m in self.hist[1:]
               if m.get("role") in ("user", "assistant")]
        out = []
        for m in raw[-n:]:
            c = re.sub(r"\[(?:img|gen|sticker|voice):[^\]]*\]", "",
                       m.get("content") or "").strip()
            # 剥完空了（她只发了个图）也得留个占位，不然 user/assistant 会连着两条
            out.append({"role": m["role"],
                        "content": c or "（发了一张图）"})
        return out

    # ---------- 阶段 A：API 出草稿 ----------
    def draft(self, user_text, memory_block):
        """出草稿。

        2026-09-29 改成**真正的多轮 messages**（以前是把对话历史拼成
        "他：…／你：…" 一段文本，塞进单条 user 消息里）。

        用户说"说完这句话下一句话就变了"，根因就在这个结构上：
        人设 + 时间 + 记忆块 + 生活块 + 日记 + 贴纸清单 本身已经是一大坨，
        最近两句话又被揉进这坨文本的中间 —— 模型注意力被前面稀释，看不牢
        （典型的 lost in the middle）。表现就是"接不上上一句"。

        现在的结构：
            system            = 人设 + 此刻 + 记忆 + 历史摘要 + 她的生活 + 贴纸 + 说话规则
            user / assistant  = 最近 12 条真实对话，交替出现
            user              = 他这次说的话
        资料归资料、对话归对话，模型才分得清"刚才聊到哪儿"。

        2026-09-29 再加一层「历史摘要」（recap_store）：12 条窗口之外的旧对话
        以前是直接丢掉的，现在被压成"聊过什么"跟着走，所以她不会聊完就忘。
        只走这条路 —— 主动搭话（speak_on_scene）和朋友圈（moment）都不带，
        那两条要的是"随口一说"，塞一堆历史反而啰嗦。
        """
        if not self.api.get("api_key"):
            return None
        parts = [self.persona_text]
        tm = self._time_hint()
        if tm:
            parts.append("\n" + tm)
        if memory_block:
            parts.append("\n" + memory_block)
        # 历史消息打了时间标签时才说明读法（没打就不花这份 token）。
        # 见 _stamp / seed_history —— 不说明她会把方括号当正文模仿。
        if any(str(m.get("content") or "").startswith("[")
               for m in self.hist[1:] if m.get("role") != "system"):
            parts.append("\n" + HIST_TIME_NOTE)
        try:
            import recap_store
            rb = recap_store.block()
            if rb:
                parts.append("\n" + rb)
        except Exception:
            pass
        # query=他这轮说的话：日记正文只在他问到的时候才带（2026-09-29）
        life = self._life_block(query=user_text)
        if life:
            parts.append("\n" + life)
        # 约定账本（agenda）：有日期的、说好的事按日期强制带上，不靠语义碰运气。
        # 2026-09-29 加 —— 实测旧机制下"票买了吗"这种短句压根不检索、
        # "放假了还上早八"排第 13 名召不回，同一件事换个说法就漏。
        # 放在生活块**之后**是故意的：冲突时它离输出更近，更能压过
        # "我们那边不放那么多假"这类日常设定（故障②就是这么来的）。
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
        msgs = [{"role": "system", "content": "".join(parts)}]
        for m in self.recent_hist():
            msgs.append(m)
        # 说话规则贴在**最后一条**（他的原话后面）—— 约束力最强。
        # 不放进 system：GLM 对 system 尾部的规则敏感度低，实测照做率明显差。
        # hist 里存的是 user_text 原值、不含这段，所以规则不会污染聊天历史。
        # 分隔线别省：不加的话规则会紧贴在他说的话后面，
        # 模型偶尔会把"只输出你会说的话本身"当成他说的内容的一部分。
        tail = (user_text + "\n\n—— 下面是回话要求（不是他说的话）——\n"
                + self.draft_rules)
        if life:
            tail += "\n" + LIFE_RULES
        msgs.append({"role": "user", "content": tail})
        try:
            r = httpx.post(
                self.api["api_base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.api["api_key"]},
                json={"model": self.api.get("model", "deepseek-chat"),
                      "messages": msgs,
                      # 90 → 200：90 个 token 大约只有 60~90 个汉字，
                      # 她想多说两句就被硬截断，看着也像"话说了半句就变"。
                      "temperature": 0.85, "max_tokens": 200,
                      # GLM 系默认先思考几百字，聊天气泡等不起 —— 关到最低档
                      "reasoning_effort": "low"},
                timeout=60,
            )
            r.raise_for_status()
            return _clean(r.json()["choices"][0]["message"]["content"],
                          keep_newlines=True)
        except Exception:
            return None

    # ---------- 朋友圈专用：不走聊天框架 ----------
    def moment(self, prompt, max_tokens=260):
        """生成一条朋友圈（要发就返回"发\\n正文…"，不发返回"无"）。

        为什么不复用 chat()（2026-09-22 踩过）：
        chat() 会把提示词包成「他现在对你说：<提示词>」，还塞进最近聊天记录、
        贴图清单和 LIFE_RULES。于是模型有时把它当成"他发来的一条消息"，
        只回一个字「发」——正文没了，朋友圈就发不出去（日志里 raw='发'）。
        单独一条干净通道：人设 + 时间 + 她的生活状态 + 提示词，稳定得多。
        不碰 self.hist，所以不需要加锁，也不该拖住 App 那边的聊天。
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

    # ---------- 对外：说一句话 ----------
    def chat(self, user_text, proactive=False, img=None, log=True):
        """返回 (她说的话, 模式标签)。模式标签用于排查到底走的哪条路

        img：这一轮带的图片路径（有的话会记进聊天存档，手机/App 那边好显示）。
        图片内容本身早就在 user_text 里被描述成文字了 —— 她看不见像素，只看得见描述。
        """
        from memory_store_v2 import MemoryStore

        memory_block = ""
        # 2026-09-29 加闸门：短句闲聊（"嗯""在吗""哈哈""??"）不检索记忆。
        # 这种话本身没信息量，检索出来的"最相关"记忆往往是硬凑的，
        # 塞进上下文反而把她的话题和情绪带跑 —— 用户反馈"下句话就变了"有它一份。
        # 阈值同时从 0.25 提到 0.40（见 memory_store_v2.MemoryStore.render）。
        q = (user_text or "").strip()
        if self.mem is not None and len(q) >= 6:
            try:
                _t1 = time.time()
                memory_block = self.mem.render(query=q, top_k=5)
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

        # 以前这里还有一层「本地 4B 兜底」（2026-09-21 移除）：
        # 本地模型要 bitsandbytes 4bit 量化，而这台机是 AMD 卡，拿不到 CUDA 版的
        # 预编译包，那层兜底从来没真正跑起来过，只是白占 13GB 磁盘。
        # 现在 API 拿不到稿就明说，不再假装还有后路。
        if draft:
            ans = draft
            self.last_mode = "API直出"
        else:
            ans = "……我脑子有点转不动，等会儿再聊？"
            self.last_mode = "API失败"
        # API 拿不到稿时的那句兜底话，绝不能当成"她说过的话"存下来。
        # 2026-09-21~25 账户欠费期间，主动搭话每 40 分钟失败一次、每次都把这句
        # 写进 chat_history.jsonl，攒了 97 条一模一样的记录；重启后 seed_history
        # 把它们灌进上下文，她的聊天历史里全是同一句话，人设就假了。
        _failed = (self.last_mode == "API失败")

        # 记忆抽取挪到后台：提取 API 一旦慢（25s 超时）也不能拖住回复
        if self.mem is not None and not proactive:
            def _extract_bg():
                try:
                    _t = time.time()
                    facts, events, _mode = MemoryStore.extract(user_text, self.api)
                    for k, v in facts:
                        self.mem.add(f"{k}：{v}", mtype="fact")
                    for e in events:
                        self.mem.add(e, mtype="event")
                    print(f"[perf] 后台记忆抽取+写入 {time.time()-_t:.1f}s "
                          f"(facts={len(facts)}, events={len(events)})", flush=True)
                except Exception as e:
                    print(f"[perf] 后台记忆抽取失败：{str(e)[:80]}", flush=True)
            threading.Thread(target=_extract_bg, daemon=True).start()

        # 主动搭话（proactive=True）的输入是「说/无」决策提示词，绝不能进对话
        # 历史——否则正常聊天会模仿这个格式，回出「说 xxx」开头（2026-09-21 事故）。
        # 历史里只留她的正文（剥掉「说」标记）；她答「无」就什么都不记。
        if proactive:
            _ls = (ans or "").splitlines()
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
            _clean = "\n".join(_ls).strip()
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
        # 存一份当天的对话流水 —— 她晚上写日记要用（不然无从下笔，只能瞎编）
        # 失败的兜底话不记：她没真的说过这句话，写进日记就是编的
        if self.life is not None and not _failed:
            self.life.log_chat("user", user_text)
            self.life.log_chat("assistant", ans)
        # 再存一份聊天记录存档 —— 重启桌宠后聊天窗能接着看，上下文也接得上
        # 朋友圈决策这类内部调用不进聊天存档（不然她的对话里会出现提示词）
        if log and not _failed:
            self._log_turn(user_text, ans, img=img)
        return ans, self.last_mode

    # ---------- 聊天记录存档（跨重启） ----------
    def _log_turn(self, user_text, ans, img=None):
        """把这一轮存进 chat_history.jsonl。写不进去也不能影响聊天。

        img 是可选的图片路径 —— 手机/App 靠它把图显示出来。
        只加在 user 那条上（她不会发图）。
        """
        try:
            t = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            u = {"t": t, "role": "user", "text": user_text}
            if img:
                u["img"] = img
            with open(CHAT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(u, ensure_ascii=False) + "\n")
                f.write(json.dumps({"t": t, "role": "assistant", "text": ans},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    @staticmethod
    def read_history(limit=40):
        """读回聊天存档，返回最近 limit 条（正序）。文件不在就返回空。"""
        if not os.path.exists(CHAT_LOG):
            return []
        out = []
        try:
            with open(CHAT_LOG, encoding="utf-8") as f:
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

    def seed_history(self, limit=16):
        """重启后把存档灌回对话历史，让她还记得刚才聊到哪儿。

        只灌最近几轮（默认 16 条消息 ≈ 8 个来回）。灌太多会让每轮 prompt
        变长、白烧 token，而且模型对太早的上下文本来也记不牢。
        返回灌进去的条数。

        2026-09-29：改成用存档里的真实时间打标签 `[09-29 06:18]`。
        存档本来就写了 t（`_log_turn` / `_cloud_append_assistant` 都写），
        以前灌回来时把 t 扔掉了 —— 于是一排"约定的那个周末"没有任何时间标记，
        模型只能当成此刻正在谈的事（这是她"把 4 天前的 25 号当未来"的直接原因）。
        """
        msgs = self.read_history(limit)
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

    @staticmethod
    def clear_history():
        """清空聊天记录存档（聊天窗里"清空聊天记录"用）"""
        try:
            os.remove(CHAT_LOG)
            return True
        except OSError:
            return False

    # ---------- 主动搭话：她看到你在干嘛，自己开口 ----------
    def speak_on_scene(self, scene):
        """基于"看到你在做什么"主动说一句。

        scene 是 watcher 给的字典：
            what   他大概在干嘛
            detail 画面具体内容
            mood   猜的心情
            hook   可以从哪里开口

        和被动回答不一样：这里是她主动开口，所以语气要更随意，
        像是瞄了一眼你的屏幕随口说一句，而不是正儿八经接话。
        """
        what = (scene.get("what") or "").strip()
        detail = (scene.get("detail") or "").strip()
        mood = (scene.get("mood") or "").strip()
        hook = (scene.get("hook") or "").strip()
        source = (scene.get("source") or "").strip()
        if not (what or detail):
            return "", "无内容"

        memory_block = ""
        if self.mem is not None:
            try:
                memory_block = self.mem.render(query=f"{what} {detail}", top_k=4)
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
        # 措辞按来源区分：手动触发就是想聊两句，感知触发就"注意到他在干嘛"。
        # （"screen" 分支 = 截屏 + 视觉模型那条路，2026-09-29 已整体移除）
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
        # 2026-09-29 跟 draft() 对齐：改成多轮 messages + 规则贴最后。
        # 这里以前只喂 4 条、且拼在单条消息里 —— 主动搭话完再正常聊天接不上，
        # 跟"下句话就变了"是同一处病根。
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

    def reset(self):
        self.hist = [{"role": "system", "content": self.persona_text}]
