# -*- coding: utf-8 -*-
"""她自己的生活

她在你不在的时候也在过日子，这个模块负责那部分。

核心是「惰性补算」而不是常驻定时器：不做"每半小时定时生成"—— 你关机时她不需要
过日子，那些调用纯属白烧。改成只在她马上要被用到的时候（打开聊天窗 / 她主动搭话 /
你打开她的日记）才把「上次记到的位置 → 现在」这段空白补出来，她睡觉那 9 小时直接
跳过。补算是幂等的：真相存在 events.jsonl 里，某个 (日期, 时段) 只要有过记录就
不再生成。

两套记忆必须分开：data/memory/ 是关于**用户**的事实，her_life/ 是她**自己**的经历。
混在一起的后果是 —— 她会把你的实习说成她的实习。所以这里产出的文本一律以"你自己
的事"开头，跟"你记得他的事"并排注入、互不相通。

目录结构：
config/world.json                     她的世界：学校 / 课表 / 室友（硬约束，不许编）
data/her_life/state.json              此刻在干嘛 · 心情
data/her_life/events.jsonl            她的经历流（一行一条）
data/her_life/chat_YYYY-MM-DD.jsonl   当天和你的对话（写日记要用）
data/journal/YYYY-MM-DD.md            她自己写的日记
"""
import os, re, json, time, datetime, threading
import httpx

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 生活片段（一天切成 6 段）。23:00 ~ 07:00 是睡觉，不生成。
SLOTS = [
    ("早上",  7 * 60,       9 * 60),
    ("上午",  9 * 60,      12 * 60),
    ("中午", 12 * 60,      14 * 60),
    ("下午", 14 * 60,      17 * 60 + 30),
    ("傍晚", 17 * 60 + 30, 19 * 60),
    ("晚上", 19 * 60,      23 * 60),
]
SLEEP = "深夜"

# 时段名 -> 该时段的大致中点（分钟），用来算"这件事过去多久了"。
# 不算的话她会把中午的事说到下午还带个"刚"字。
_SLOT_MID = {
    "早上": 8 * 60,
    "上午": 10 * 60 + 30,
    "中午": 13 * 60,
    "下午": 15 * 60 + 45,
    "傍晚": 18 * 60 + 15,
    "晚上": 21 * 60,
}


def _slot_gap_text(slot, now):
    """时段名 -> 距现在大概多久（'约4小时前'）。1.5 小时内算新鲜，返回空。"""
    mid = _SLOT_MID.get(slot)
    if mid is None:
        return ""
    cur = now.hour * 60 + now.minute
    diff = cur - mid
    if diff < 90:
        return ""
    h = diff / 60.0
    return "约1小时前" if h < 1.5 else f"约{int(round(h))}小时前"

# 一天的「底色」，按日期轮换。按日期取模选，同一天重算几次都是同一个
TONE_HINTS = (
    "平平常常的一天，没什么特别的事",
    "很普通的一天，但有件小事让你挺开心",
    "有点累，事情都堆在一起",
    "有点烦，跟人闹了点小别扭，或者做什么都不太顺手",
    "状态挺好，做什么都来劲",
    "有点闷，有点想家、想以前的事",
    "又忙又乱，不过到了晚上总算松了口气",
    "普普通通，累是有点累，但也说不上有什么可抱怨的",
)

DIARY_HOUR, DIARY_MIN = 22, 30      # 过了这个点，当天就可以写日记了
MAX_DETAIL_SLOTS = 6                # 一次补算最多细写几个片段（≈一天）
MAX_BACKFILL_DAYS = 3               # 最多往前补几天
MAX_DIARY_BACKFILL = 3              # 一次最多补写几篇日记
MIN_INTERVAL = 60                   # 两次补算的最小间隔（秒），防抖动

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import paths
    LIFE_DIR = paths.LIFE_DIR            # data/her_life
    JOURNAL_DIR = paths.JOURNAL_DIR      # data/journal
    WORLD_PATH = paths.WORLD_PATH        # config/world.json（她的设定，你要改的那个）
    LOCK_PATH = paths.CATCHUP_LOCK       # data/run/catchup.lock
except Exception:                        # 兜底：路径规则变了也不至于起不来
    LIFE_DIR = os.path.join(_HERE, "data", "her_life")
    JOURNAL_DIR = os.path.join(_HERE, "data", "journal")
    WORLD_PATH = os.path.join(_HERE, "config", "world.json")
    LOCK_PATH = os.path.join(_HERE, "data", "run", "catchup.lock")

# 跨进程锁：桌宠和其它入口是两个进程，可能同时开着。threading.Lock 管不了
# 跨进程，两边一起补算会重复烧 token、还可能写出重复经历
LOCK_STALE = 300                    # 秒：补算最多几十秒，超过就当成残留锁

DEFAULT_WORLD = {
    "_说明": (
        "这是角色自己的世界，她的生活按这里写的来。"
        "schedule 的键是星期（1=周一 … 7=周日），值是 [开始-结束, 课名, 地点] 的列表；"
        "roommates 里每人一个名字和一句性格；hangouts 是她常去的地方。"
        "这些是硬设定 —— 她不会编出这里没有的课、没提到的室友。"
        "改完想让她重新开始：删掉 her_life/events.jsonl 和 state.json 就行。"
    ),
    "school": "",
    "city": "",
    "grade": "大三",
    "major": "",
    "start_year": 2024,
    "dorm": "",
    "roommates": [],
    "schedule": {},
    "routine": {
        "07:20": "起床",
        "12:00": "午饭",
        "17:40": "晚饭",
        "23:00": "睡觉",
    },
    "hangouts": [],
    "clubs": [],
    "notes": "",
}


# --- 让模型生成世界 ---
# 代码随机只有写死的十几个专业 / 城市可选，一眼模板感。本地 27B 优先（零成本），
# 云端兜底，都不可用才回退代码随机
_WORLD_PROMPT = (
    "你是角色设定生成器。帮虚拟陪伴角色随机生成一套真实可信的中国女大学生活设定。\n\n"
    "要求：\n"
    "- 她是中国某所普通本科或学院的大三女生。城市和专业要真实存在、尽量多样：\n"
    "  不要总给计算机、汉语言这种常见专业，可以冷门些（茶学、蚕学、听力与言语康复、\n"
    "  葡萄与葡萄酒工程、宝石设计与工艺、书法学、档案学、水族科学与技术……\n"
    "  但必须是真实存在的专业）。\n"
    "- 学校写成「城市名+类型后缀」（师范大学 / 医科大学 / 理工大学 / 财经大学 / 学院 / 大学），城市要真实。\n"
    "- 室友 1~2 个，各给名字和一句性格；社团 1~2 个（名字+活动时间）；常去地点 3~4 个，\n"
    "  要像真学生日常（图书馆、某食堂、后街、操场、奶茶店、自习室……）。\n"
    "- schedule 课表必须按她的专业来，从真实课程里挑；周一~周五每天 1~3 节，\n"
    "  时间落在 08:00-17:40（用 '08:00-09:40' 这种格式），周末通常空（键 '6' '7' 写 []）。\n"
    "  课名要跟专业对得上。\n"
    "- routine 固定为 {\"07:20\":\"起床洗漱\",\"07:50\":\"去食堂吃早饭\",\"12:00\":\"午饭\",\"17:40\":\"晚饭\",\"23:00\":\"熄灯睡觉\"}\n"
    "- notes 写一句她最近的小状态（备考 / 找实习 / 养了宠物 / 纠结考研之类）。\n"
    "- 整体自然、有生活气息，不要八股。\n\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段如下：\n"
    "{\"school\":\"\",\"city\":\"\",\"grade\":\"大三\",\"major\":\"\",\"start_year\":2024,\n"
    "\"dorm\":\"\",\"roommates\":[{\"name\":\"\",\"trait\":\"\"}],\n"
    "\"schedule\":{\"1\":[[\"08:00-09:40\",\"课名\",\"地点\"]],\"2\":[],\"3\":[],\"4\":[],\"5\":[],\"6\":[],\"7\":[]},\n"
    "\"routine\":{\"07:20\":\"起床洗漱\",\"07:50\":\"去食堂吃早饭\",\"12:00\":\"午饭\",\"17:40\":\"晚饭\",\"23:00\":\"熄灯睡觉\"},\n"
    "\"hangouts\":[],\"clubs\":[{\"name\":\"\",\"when\":\"\"}],\"notes\":\"\"}"
)


def _lmstudio_reachable():
    """本地 LM Studio 的 OpenAI 兼容端点（默认 1234）在不在。"""
    try:
        import socket
        s = socket.socket()
        s.settimeout(1.2)
        s.connect(("127.0.0.1", 1234))
        s.close()
        return True
    except Exception:
        return False


def _gen_world_http(base_url, api_key, model, max_tokens=900, timeout=60):
    """调某个 OpenAI 兼容端点生成 world，解析 + 校验 + 用 DEFAULT_WORLD 补全。失败返回 None。"""
    try:
        import httpx
        r = httpx.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key},
            json={"model": model,
                  "messages": [{"role": "user", "content": _WORLD_PROMPT}],
                  "temperature": 0.95, "max_tokens": max_tokens},
            timeout=timeout,
        )
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        return None

    # 抠 JSON（模型可能夹带解释或 ```json 围栏）
    try:
        a = text.find("{")
        b = text.rfind("}")
        if a < 0 or b <= a:
            return None
        raw = json.loads(text[a:b + 1])
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None

    w = dict(DEFAULT_WORLD)
    for k in ("school", "city", "major", "dorm", "notes"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            w[k] = v.strip()
    # 专业/学校/城市缺一就不采纳，让上层回退
    if not (w.get("school") and w.get("city") and w.get("major")):
        return None
    rms = raw.get("roommates")
    if isinstance(rms, list):
        good = [{"name": str(r0["name"]).strip(),
                 "trait": str(r0.get("trait", "")).strip()}
                for r0 in rms[:2]
                if isinstance(r0, dict) and r0.get("name")]
        if good:
            w["roommates"] = good
    cls = raw.get("clubs")
    if isinstance(cls, list):
        good = [{"name": str(c0["name"]).strip(),
                 "when": str(c0.get("when", "")).strip()}
                for c0 in cls[:2]
                if isinstance(c0, dict) and c0.get("name")]
        if good:
            w["clubs"] = good
    hg = raw.get("hangouts")
    if isinstance(hg, list):
        items = [str(x).strip() for x in hg if str(x).strip()]
        if items:
            w["hangouts"] = items[:4]
    sched = raw.get("schedule")
    if isinstance(sched, dict):
        out = {}
        for d in ("1", "2", "3", "4", "5", "6", "7"):
            rows = sched.get(d)
            out[d] = [ [str(x).strip() for x in row[:3] if x is not None]
                       for row in rows if isinstance(row, (list, tuple)) and len(row) >= 2 ] \
                     if isinstance(rows, list) else []
        w["schedule"] = out
    if not w.get("grade"):
        w["grade"] = "大三"
    if not w.get("start_year"):
        w["start_year"] = 2024
    return w


def _world_note(src):
    return (
        "这是角色自己的世界，她的生活严格按这里写的来。"
        "schedule 的键是星期（1=周一 … 7=周日），值是 [开始-结束, 课名, 地点] 的列表；"
        "roommates 里每人一个名字和一句性格；hangouts 是她常去的地方。"
        "这些是硬设定 —— 她不会编出这里没有的课、没提到的室友。"
        "这份由%s随机生成（专业、学校、课表都按真实来），想固定就手动改这里，改完重启桌宠。"
        % src
    )


def generate_world_via_llm(api, timeout=180):
    """让模型生成一份世界设定。本地 LM Studio（localhost:1234）优先，否则云端，都不行返回 None。"""
    # 1) 本地 LM Studio（零成本，27B 质量够生成一份设定）
    if _lmstudio_reachable():
        w = _gen_world_http("http://localhost:1234/v1", "lm-studio", "local-model",
                            max_tokens=900, timeout=timeout)
        if w:
            w["_说明"] = _world_note("本地模型")
            return w
    # 2) 云端 API
    if (api or {}).get("api_key"):
        w = _gen_world_http((api.get("api_base") or "https://api.deepseek.com"),
                            api["api_key"], api.get("model", "deepseek-chat"),
                            max_tokens=900, timeout=min(timeout, 60))
        if w:
            w["_说明"] = _world_note("云端模型")
            return w
    return None


# --- 时间工具 ---
def _dt(d, minutes):
    return datetime.datetime.combine(d, datetime.time(minutes // 60, minutes % 60))


def slot_of(dt):
    """现在属于哪个生活片段。返回 (片段名, 是否在睡觉)"""
    m = dt.hour * 60 + dt.minute
    for name, a, b in SLOTS:
        if a <= m < b:
            return name, False
    return SLEEP, True


def slots_between(start, end):
    """(start, end] 之间已经开始的片段，按时间顺序。end 之后的片段不要。"""
    out = []
    d = start.date()
    while d <= end.date():
        for name, a, b in SLOTS:
            s, e = _dt(d, a), _dt(d, b)
            if s >= end:
                return out
            if e <= start:
                continue
            out.append({"date": d.strftime("%Y-%m-%d"), "slot": name, "start": s, "end": e})
        d += datetime.timedelta(days=1)
    return out


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def _say(when, total_minutes):
    """把分钟数说成人话"""
    h, m = divmod(int(total_minutes), 60)
    if h and m:
        return f"{h} 小时 {m} 分钟"
    if h:
        return f"{h} 小时"
    return f"{m} 分钟"


def _strip_self(s):
    """去掉开头的"我 / 我正在" —— 这句要拼成「你此刻：…」，留着人称就串了。"""
    s = (s or "").strip()
    return re.sub(r"^(?:我(?:现在|此刻|正|正在)?|现在|此刻)\s*", "", s)


# --- 生活引擎 ---
class LifeEngine:
    """她自己的生活。所有写盘都在这一个类里，外面只管调 catch_up / life_block。"""

    def __init__(self, api_config=None, home=None, timeout=40, name="", persona=""):
        cfg = (api_config or {}).get("life") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.diary_hour = int(cfg.get("diary_hour", DIARY_HOUR))
        self.diary_min = int(cfg.get("diary_minute", DIARY_MIN))
        self.api = api_config or {}
        self.timeout = timeout
        self.name = name
        # 当前人设的 key。写进 state.json，用来判断"这份生活是谁留下的"——
        # 换人设后生活不会自动跟着换，得让人知道该重置（见 is_stale）
        self.persona = persona or ""
        self.home = home or LIFE_DIR
        # 自定义 home 只给测试用，journal 挨着它放。world.json 单独在 config/
        # 下（属于"你会手动改的设定"，不跟经历流混一起）
        self.journal_dir = os.path.join(home, "journal") if home else JOURNAL_DIR
        self.world_path = WORLD_PATH
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.journal_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._last_run = 0.0
        self.last_result = {}
        self.world = self.load_world()

    # --- 她的世界 ---
    def load_world(self):
        # 没有就生成一份（本地 27B 优先 / 云端兜底），都不行才代码随机
        if not os.path.exists(self.world_path):
            w = self._gen_world_first_time()
            if w is not None:
                self.save_world(w)
                print("[世界] 已用模型随机生成她的世界（%s · %s · %s）"
                      % (w["city"], w["school"], w["major"]), flush=True)
                return w
            try:
                from worldgen import build_world
                w = build_world()
                self.save_world(w)
                print("[世界] 模型/代码生成都失败，已用代码随机兜底（%s · %s · %s）"
                      % (w["city"], w["school"], w["major"]), flush=True)
                return w
            except Exception as e:
                print("[世界] 随机生成失败，退回内置空壳：%s" % e, flush=True)
                self.save_world(DEFAULT_WORLD)
                return dict(DEFAULT_WORLD)
        try:
            w = json.load(open(self.world_path, encoding="utf-8"))
        except Exception:
            return dict(DEFAULT_WORLD)
        merged = dict(DEFAULT_WORLD)
        merged.update(w or {})
        return merged

    def _gen_world_first_time(self):
        try:
            return generate_world_via_llm(self.api, timeout=180)
        except Exception as e:
            print("[世界] 模型生成世界失败：%s" % e, flush=True)
            return None

    def regenerate_world(self, seed=None):
        """重新生成一份身份并落盘。优先让模型生成（本地 27B / 云端，更自然），都失败才代码随机。
        调用方负责：世界一变，旧的经历 / 日记会穿帮，要清掉重来。"""
        w = self._gen_world_first_time()
        if w is None:
            from worldgen import build_world
            w = build_world(seed)
        self.save_world(w)
        self.world = w
        return w

    def save_world(self, world):
        # newline="\n" 不能省。不给的话 Windows 文本模式写成 CRLF，云端那份就跟
        # 仓库那份天天"字节不一致、内容一模一样"，比对时非常误导
        json.dump(world, open(self.world_path, "w", encoding="utf-8", newline="\n"),
                  ensure_ascii=False, indent=2)
        self.world = world

    def world_ready(self):
        """学校 / 专业 / 室友 有没有填。没填也能跑，但一致性差很多。"""
        w = self.world
        return bool(w.get("school") and w.get("major"))

    def _world_block(self, date_str=None):
        """她的基本情况。date_str 给了就顺带说明"这天在不在假期里"。

        为什么假期必须放这个块里：它是聊天、写日记、生成生活片段**都会看到**的
        唯一一块，放这儿等于一次覆盖所有出口。否则就会出现"校园空了她还在上课"
        那种自相矛盾 —— 课表是硬约束塞进去的，模型不敢违背，只好两边都写。
        """
        w = self.world
        bits = []
        where = "".join(x for x in [w.get("city", ""), w.get("school", "")] if x)
        if where:
            bits.append(f"- 你在{where}读{w.get('grade','')}，专业是{w.get('major','')}")
        elif w.get("grade"):
            bits.append(f"- 你是{w['grade']}大学生")
        if w.get("dorm"):
            bits.append(f"- 你住{w['dorm']}")
        for r in w.get("roommates") or []:
            if isinstance(r, dict) and r.get("name"):
                bits.append(f"- 室友{r['name']}：{r.get('trait','')}")
            elif isinstance(r, str):
                bits.append(f"- 室友{r}")
        if w.get("hangouts"):
            bits.append("- 你常去：" + "、".join(w["hangouts"]))
        for c in w.get("clubs") or []:
            if isinstance(c, dict) and c.get("name"):
                bits.append(f"- 社团：{c['name']}（{c.get('when','')}）")
            elif isinstance(c, str):
                bits.append(f"- 社团：{c}")
        if w.get("notes"):
            bits.append("- " + str(w["notes"]))
        # 天气只读缓存，城市跟他的 IP 走 —— 是"他那边"的天气，她拿来关心他。
        # 取不到就不提，聊天不为它多等一秒
        try:
            import features as _feat
            if _feat.on("weather"):
                import weather_cache
                wx = weather_cache.note()
                if wx:
                    loc = weather_cache.city() or "他那边"
                    bits.append(f"- 他那边（{loc}）现在的天气：{wx}")
        except Exception:
            pass
        # 热搜措辞是"她刚刷到"，不是新闻播报，别让她念榜单
        try:
            import features as _feat
            if _feat.on("hotboard"):
                import hotboard_cache
                hot = hotboard_cache.note()
                if hot:
                    bits.append("- 你刚才刷手机看到的热搜（你想聊哪个就聊哪个，"
                                "也可以完全不提，别一条条播报）：" + hot)
        except Exception:
            pass
        ds = date_str or datetime.date.today().isoformat()
        br = self._break_of(ds)
        if br:
            bits.append(f"- 你现在在{br.get('name', '假期')}里"
                        f"（{br.get('start')}~{br.get('end')}），学校不上课，一天都是你自己的")
            # 假期进度由代码算好。让模型自己算日期的话，他会顺着对方说
            # —— 他把第一天说成最后一天，她就接了
            try:
                s = datetime.date.fromisoformat(br["start"])
                e = datetime.date.fromisoformat(br["end"])
                nth = (datetime.date.fromisoformat(ds) - s).days + 1
                back = e + datetime.timedelta(days=1)
                wdn = "一二三四五六日"[back.isoweekday() - 1]
                bits.append(f"- 假期共{(e - s).days + 1}天，今天是第{nth}天，"
                            f"{back.month}月{back.day}日（周{wdn}）开学。"
                            "他说的日期跟这里对不上时按这里为准，可以直接纠正他")
            except Exception:
                pass
            # 假期人在哪。不写的话她默认还待在学校（国庆待在空宿舍）
            hp = self.world.get("holiday_plan") or {}
            if (hp.get("text") and hp.get("start") and hp.get("end")
                    and hp["start"] <= ds <= hp["end"]):
                bits.append(f"- 这次的安排：{hp['text']}，说话做事按这个来")
        elif not self._calendar_known(ds):
            # 校历没维护到这天，让她含糊其辞而不是编个笃定的答案
            bits.append("- 这天的放假安排还没出通知，你自己也不确定，"
                        "别把有没有课、放不放假说死，要用「应该 / 大概 / 还不确定」的口气")
        return "\n".join(bits)

    # --- 校历：放假没课，调休日按指定星期补课 ---
    def _calendar(self):
        return self.world.get("calendar") or {}

    def _semesters(self):
        """学期列表（按开始日排序，去掉缺起止的脏数据）。寒暑假就从这里推导。"""
        sems = [s for s in self._calendar().get("semesters") or []
                if s.get("start") and s.get("end")]
        return sorted(sems, key=lambda s: s["start"])

    def _semester_gap_of(self, date_str):
        """这天落在两个学期之间吗（寒暑假）。返回假 None / {"name","start","end","derived"}。

        寒暑假走推导而不是写死：学期起止一年只动一次，而寒暑假的起止年年不同，
        手填容易忘改。        法定节假日不能这么算 —— 调休是国务院每年拍板公布的，
        没有规律（2026 年清明端午中秋都不调，春节却拼出 9 天），只能留在 breaks 逐年填。
        """
        sems = self._semesters()
        for a, b in zip(sems, sems[1:]):
            try:
                gs = datetime.date.fromisoformat(a["end"]) + datetime.timedelta(days=1)
                ge = datetime.date.fromisoformat(b["start"]) - datetime.timedelta(days=1)
            except Exception:
                continue
            if gs <= ge and gs.isoformat() <= date_str <= ge.isoformat():
                return {"name": "寒假" if gs.month in (12, 1, 2) else "暑假",
                        "start": gs.isoformat(), "end": ge.isoformat(),
                        "derived": True}
        # 学期列表的"尾巴"后面没有下一条学期来框住暑假，按惯例推到 8/31
        if sems:
            try:
                gs = datetime.date.fromisoformat(sems[-1]["end"]) + datetime.timedelta(days=1)
            except Exception:
                return None
            ge = datetime.date(gs.year, 8, 31)
            if gs.month in (5, 6, 7) and gs <= ge and gs.isoformat() <= date_str <= ge.isoformat():
                return {"name": "暑假", "start": gs.isoformat(), "end": ge.isoformat(),
                        "derived": True}
        return None

    def _calendar_known(self, date_str):
        """校历对这天有没有结论（在学期里 / 放假 / 调休）。

        没结论 = 校历还没维护到这个日期。宁可让她说「通知还没出」，
        也别让她把编出来的课表说得跟真的一样 —— 那才是穿帮。
        """
        if self._break_of(date_str) or self._makeup_of(date_str):
            return True
        for s in self._semesters():
            if s["start"] <= date_str <= s["end"]:
                return True
        return False

    def _break_of(self, date_str):
        """这天落在哪个假期区间里（不在假期返回 None）。

        顺序：breaks 里的法定假日优先（精确数据），再推导学期间隙（寒暑假）。
        项目原本**只有"星期几"这一维、没有"日期"**，10/1 是周四就直接套周四课表，
        于是她在国庆当天上课、交报告、被老师扣分。
        """
        for b in self._calendar().get("breaks") or []:
            s, e = b.get("start"), b.get("end")
            if s and e and s <= date_str <= e:
                return b
        return self._semester_gap_of(date_str)

    def _makeup_of(self, date_str):
        """这天是不是调休补课日（不是返回 None）。"""
        for m in self._calendar().get("makeup") or []:
            if m.get("date") == date_str:
                return m
        return None

    def _classes_on(self, wd):
        """按星期几取课表，wd 是 1~7。"""
        rows = (self.world.get("schedule") or {}).get(str(wd)) or []
        out = []
        for r in rows:
            if isinstance(r, (list, tuple)):
                out.append(" ".join(str(x) for x in r if x))
            else:
                out.append(str(r))
        return "；".join(out)

    def _classes_of(self, date_str):
        """某天的课表（硬约束）。假期一律没课；调休补课日按 as_weekday 补；
        校历没覆盖到的日子也返回空 —— 课表是硬约束，编出来的课就是穿帮。
        """
        if self._break_of(date_str):
            return ""
        mk = self._makeup_of(date_str)
        if mk:
            wd = str(mk.get("as_weekday") or "")
            return self._classes_on(wd) if wd.isdigit() else ""
        if not self._calendar_known(date_str):
            return ""
        try:
            return self._classes_on(datetime.date.fromisoformat(date_str).isoweekday())
        except Exception:
            return ""

    # --- 状态 / 事件 ---
    @property
    def state_path(self):
        return os.path.join(self.home, "state.json")

    @property
    def events_path(self):
        return os.path.join(self.home, "events.jsonl")

    def state(self):
        try:
            return json.load(open(self.state_path, encoding="utf-8")) or {}
        except Exception:
            return {}

    def _save_state(self, st):
        json.dump(st, open(self.state_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    # --- 这份生活是谁留下的 ---
    def is_stale(self):
        """生活痕迹是不是另一套人设留下的。

        比对的是 state.json 里记的 persona 标记，不去猜内容里有没有旧身份
        （关键词匹配漏一个就穿帮）。两种情况不算旧：还没开始过日子（没东西
        可保留），以及标记一致。老版本写的数据没有标记，一律当旧的问一句。
        """
        try:
            if not os.path.exists(self.events_path) or os.path.getsize(self.events_path) == 0:
                return False
        except OSError:
            return False
        return (self.state().get("persona") or "") != (self.persona or "")

    def reset_life(self, keep_chat=True):
        """清掉她的生活痕迹，让她从今天重新过日子。先备份，可还原。

        清的都是"她自己的日子"：生活流、日记、朋友圈、历史摘要、日程。
        记忆库和聊天存档默认留着 —— 换人设不等于失忆，她还记得你们聊过。
        删完不重启：惰性补算会在下一轮把今天重新过一遍。
        """
        import shutil
        # 从 self.home 往上推 data 目录，别写死 paths.DATA_DIR ——
        # 那样测试只能在真实数据上跑，而这个函数是删数据的
        base = os.path.dirname(os.path.abspath(self.home))
        bak = os.path.join(base, "_bak_life_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(bak, exist_ok=True)
        gone = []
        # 日记目录要用 self.journal_dir：home 传了的话它就在 home 下面，
        # 拼 base+"journal" 会清错地方（os.remove 删目录还会静默失败）
        for src, tag in ((self.home, "her_life"), (self.journal_dir, "journal"),
                         (os.path.join(base, "summary"), "summary")):
            if not os.path.isdir(src):
                continue
            shutil.copytree(src, os.path.join(bak, tag), dirs_exist_ok=True)
            # 只清内容不删目录本身：journal 目录在 home 下面，
            # 先 rmtree(home) 会把它一起带走，后面那轮就当它不存在了
            for f in os.listdir(src):
                fp = os.path.join(src, f)
                if os.path.isdir(fp):
                    shutil.rmtree(fp, ignore_errors=True)
                else:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
            gone.append(tag)
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.journal_dir, exist_ok=True)
        for rel in ("moments.jsonl", "agenda.json") + (() if keep_chat else ("chat_history.jsonl",)):
            src = os.path.join(base, rel)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(bak, rel))
                os.remove(src)
                gone.append(rel)
        # 空壳留着，list_moments 之类有 OSError 兜底，但重建时更稳
        for rel in ("moments.jsonl", "chat_history.jsonl"):
            open(os.path.join(base, rel), "a", encoding="utf-8").close()
        self._last_run = 0.0
        return {"backup": bak, "cleared": gone}

    def events(self, days=None):
        """全部经历，按时间顺序。days 给了就只返回最近几天的。"""
        if not os.path.exists(self.events_path):
            return []
        out = []
        try:
            with open(self.events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except OSError:
            return []
        if days:
            cut = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
            out = [e for e in out if (e.get("date") or "") >= cut]
        return out

    def _append_events(self, items):
        with open(self.events_path, "a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    def _done_pairs(self):
        """已经记录过的 (日期, 片段)，补算时用来去重"""
        return {(e.get("date"), e.get("slot")) for e in self.events(days=MAX_BACKFILL_DAYS + 2)}

    # --- 当天的对话（写日记的素材） ---
    def _chat_path(self, date_str):
        return os.path.join(self.home, f"chat_{date_str}.jsonl")

    def log_chat(self, role, text):
        """role: "user" 或 "assistant"。brain 每次说完话调这里。"""
        text = (text or "").strip()
        if not text:
            return
        try:
            rec = {"t": datetime.datetime.now().strftime("%H:%M"), "role": role, "text": text}
            with open(self._chat_path(datetime.date.today().isoformat()), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def today_chat(self, date_str=None):
        date_str = date_str or datetime.date.today().isoformat()
        p = self._chat_path(date_str)
        if not os.path.exists(p):
            return []
        out = []
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            continue
        except OSError:
            return []
        return out

    # --- 调模型 ---
    def _llm(self, prompt, max_tokens=500, temperature=0.9):
        if not self.api.get("api_key"):
            return None
        try:
            r = httpx.post(
                self.api["api_base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + self.api["api_key"]},
                json={"model": self.api.get("model", "deepseek-chat"),
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": temperature, "max_tokens": max_tokens},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception:
            return None

    def _voice(self):
        return ("说话口语化、短句，像个真实的女孩子，不要有作文腔；"
                "性格活泼、有点小傲娇但也温柔")

    # --- 生成一天的片段 ---
    def _day_prompt(self, date_str, slots, done_events):
        wd = WEEKDAY[datetime.date.fromisoformat(date_str).weekday()]
        names = [s["slot"] for s in slots]
        is_today = (date_str == datetime.date.today().isoformat())
        tone = TONE_HINTS[datetime.date.fromisoformat(date_str).toordinal()
                          % len(TONE_HINTS)]
        p = [
            f"你是{self.name}。下面要记录你自己的一天，"
            f"日期是 {date_str}（{wd}）。{self._voice()}。",
            "",
            "关于你的设定（这些是固定的，别改也别编新的）：",
            self._world_block(date_str) or "- 你是个大三学生",
            f"- 这一天的基调：{tone}（照这个来，写的事要能让人感觉到）",
        ]
        cls = self._classes_of(date_str)
        if cls:
            p.append(f"- 这天你的课：{cls}")
        br = self._break_of(date_str)
        recent = [e for e in done_events if e.get("date") == date_str]
        if recent:
            p.append("")
            p.append("这天你已经记过的（别重复）：")
            for e in recent:
                p.append(f"  [{e['slot']}] {e['text']}")
        p += [
            "",
            f"现在补记这些时段：{'、'.join(names)}",
            "",
            "要求：",
            "- 每个时段写 1 件事，要有具体细节（一个动作、别人说的一句原话、一个味道），"
            "不要写成大纲（✗“上午上课，中午吃饭”）",
            "- 你是个普通的大三女生，不是每天都在过顺顺当当的日子。上面那条基调要真的透出来："
            "可以是被谁逗笑、吃到想吃的东西那种开心，也可以是答不上问题、被吵得没睡好、"
            "事情堆一起那种烦和累。别每个时段都平铺直叙地说“做了什么”",
        ]
        if br:
            # derived = 寒暑假是按学期排推导的，提示里交代一句免得她心虚
            tag = "（按学期排的）" if br.get("derived") else ""
            p.append(f"- 你在{br.get('name', '假期')}里{tag}，别写上课、老师、实验室、"
                     "作业、交报告 —— 学校根本没人")
            # 光说"别写上课"她容易写干巴巴的"在家休息"，把该干的事摆出来才像人话
            p.append("- 写放假真会做的事：追剧、打游戏、回家、逛街、跟朋友瞎逛、睡到中午；"
                     "寒假可以惦记过年、收红包、被亲戚问成绩，暑假可以嫌热，"
                     "临开学可以不想开学 —— 作息乱一点也没关系")
        p.append("- 严格照下面这个格式输出，一行一个，只写这些时段，别多写别少写：")
        for n in names:
            p.append(f"[{n}] 这个时段发生了什么")
        p.append("心情: 一句话说说你这一天心里的感觉（20 字以内，"
                 "比如“本来挺烦的，晚上吃到炸鸡好点了”）")
        if is_today:
            p.append("现在: 一句话，说你此刻正在做什么（不要用“我”开头）")
        p += [
            "",
            "另外：这一天是你自己过的。不要提到任何人在问你什么，"
            "不要出现“用户”“他”“对方”这样的字眼。",
            "不要输出格式之外的东西，不要 markdown，不要序号。",
        ]
        return "\n".join(p)

    def _parse_day(self, text, names):
        evs, mood, doing = [], "", ""
        for line in (text or "").splitlines():
            line = line.strip().lstrip("-*• ").strip()
            if not line:
                continue
            m = re.match(r"^[\[【]\s*([^\]】]+?)\s*[\]】]\s*(.+)$", line)
            if m:
                slot, body = m.group(1).strip(), m.group(2).strip()
                if slot in names and body:
                    evs.append({"slot": slot, "text": body})
                continue
            m = re.match(r"^(心情|现在)\s*[:：]\s*(.+)$", line)
            if m:
                if m.group(1) == "心情":
                    mood = m.group(2).strip()
                else:
                    doing = m.group(2).strip()
        return evs, mood, doing

    def _gen_day(self, date_str, slots, done_events):
        names = [s["slot"] for s in slots]
        txt = self._llm(self._day_prompt(date_str, slots, done_events))
        if txt is None:
            return None
        evs, mood, doing = self._parse_day(txt, names)
        if not evs:
            return None
        doing = _strip_self(doing)
        base = slots[0]["start"]
        records = []
        for i, e in enumerate(evs):
            # 时间戳按顺序摊在这一天的可用区间里，方便排序和显示
            t = base + datetime.timedelta(minutes=40 * i)
            records.append({"date": date_str, "slot": e["slot"], "text": e["text"],
                            "t": _fmt(min(e.get("_t", t), datetime.datetime.now()))})
        return {"events": records, "mood": mood, "doing": doing}

    def _gen_summary(self, slots, done_events):
        """太久没来（超过一天）时，前面的日子只给一句概括，不补细节"""
        a, b = slots[0]["date"], slots[-1]["date"]
        p = [
            f"你是{self.name}。{self._voice()}。",
            "关于你的设定（固定，别改）：",
            self._world_block() or "- 你是个大三学生",
            "",
            f"从 {a} 到 {b} 这几天你都在过你的日子。用一句话概括这几天，"
            "要有具体细节，不要罗列日程。",
            "只输出这一句话，不要格式标记，不要提到任何人在问你什么。",
        ]
        txt = self._llm("\n".join(p), max_tokens=120)
        if not txt:
            return None
        txt = txt.splitlines()[0].strip().lstrip("-*• ").strip()
        return {"date": b, "slot": "这些天", "text": txt, "t": _fmt(slots[-1]["end"])}

    # --- 日记 ---
    def diary_path(self, date_str):
        return os.path.join(self.journal_dir, f"{date_str}.md")

    def has_diary(self, date_str):
        return os.path.exists(self.diary_path(date_str))

    def list_diaries(self):
        if not os.path.isdir(self.journal_dir):
            return []
        out = []
        for n in os.listdir(self.journal_dir):
            if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", n):
                out.append(n[:-3])
        return sorted(out)

    def read_diary(self, date_str):
        try:
            raw = open(self.diary_path(date_str), encoding="utf-8").read()
        except OSError:
            return ""
        # 去掉存盘时写的日期标题行（窗口自己会显示日期）
        lines = raw.splitlines()
        if lines and lines[0].startswith("#"):
            lines = lines[1:]
        return "\n".join(lines).strip()

    def _diary_prompt(self, date_str, evs, chats):
        wd = WEEKDAY[datetime.date.fromisoformat(date_str).weekday()]
        p = [
            f"你是{self.name}。{self._voice()}。",
            "",
            "关于你的设定（固定，别改）：",
            self._world_block(date_str) or "- 你是个大三学生",
            "",
            f"现在是 {date_str}（{wd}）晚上，你要写今天的日记。",
        ]
        mood = (self.state().get("moods") or {}).get(date_str, "")
        if mood:
            p.append(f"你这一天心里的感觉：{mood}")
        if evs:
            p.append("今天你自己经历的事：")
            for e in evs:
                p.append(f"  {e['slot']}：{e['text']}")
        if chats:
            p.append("")
            p.append("今天你俩在网上说过的原话（这些只是素材 —— 日记里要用「他」转述，"
                     "别写成跟他对话；没标「他」的就是他今天没吭声）：")
            for c in chats[-16:]:
                who = "他" if c.get("role") == "user" else "我"
                p.append(f"  {who}：{c.get('text','')}")
        p += [
            "",
            "要求：",
            "- 第一人称，写给自己看的那种，随便一点、不用完整；不要标题、序号、markdown 标记",
            "- 别把今天的事一件件数一遍。只挑 1~2 件最想记的写，其余不提",
            "- 分 2~3 段，段与段之间空一行；每段两句话就够，整篇 200 字以内",
            "- 你就是个普通的女大学生：今天要是开心就写开心，不爽、累、委屈也可以写，"
            "别硬撑着写得体面。心情平就平着写",
            "- 至少有一个具体细节（一个动作、一个味道、一句原话）",
            "- 你俩隔着老远，所有话都是在网上说的，别写成见面；也别编今天没发生的事、"
            "别改上面经历里的时间和地点",
            "- 他今天说过什么、没说过什么，以上面列出的为准 —— 他没吭声就别在日记里让他说话",
            "- 日记是写给自己看的，不是写给他的：提到他用「他」转述，别对着他说"
            "（别用「你」指他）",
            "- 只输出日记正文",
        ]
        return "\n".join(p)

    def write_diary(self, date_str):
        """给她某一天写一篇日记。返回正文（失败返回 ""）"""
        evs = [e for e in self.events(days=MAX_BACKFILL_DAYS + 2) if e.get("date") == date_str]
        chats = self.today_chat(date_str)
        if not evs and not chats:
            return ""
        body = self._llm(self._diary_prompt(date_str, evs, chats), max_tokens=300)
        if not body:
            return ""
        body = re.sub(r"^#+\s*.*\n+", "", body).strip()
        if not body:
            return ""
        wd = WEEKDAY[datetime.date.fromisoformat(date_str).weekday()]
        with open(self.diary_path(date_str), "w", encoding="utf-8") as f:
            f.write(f"# {date_str} {wd}\n\n{body}\n")
        return body

    # --- 主入口：惰性补算 ---
    def _floor_date(self, now):
        """她「开始过日子」的那天。第一次跑就是今天，之后写进 state.json 永久记住。

        没有这个锚点会有个很别扭的 bug：她今天才装上，第二次补算却会把前 3 天"她还没存在"
        的日子也编出来。
        """
        s = self.state().get("started")
        if s:
            try:
                return datetime.date.fromisoformat(s)
            except Exception:
                pass
        return now.date()

    def _begin(self, now):
        """补算的起点：不早于她开始那天，也不早于 MAX_BACKFILL_DAYS 天前。"""
        recent = (now - datetime.timedelta(days=MAX_BACKFILL_DAYS)).date()
        d = max(self._floor_date(now), recent)
        return datetime.datetime.combine(d, datetime.time(0, 0))

    def needs_catch_up(self):
        """有没有落后（不调模型，纯判断，可以随便调）"""
        now = datetime.datetime.now()
        done = self._done_pairs()
        begin = self._begin(now)
        for s in slots_between(begin, now):
            if (s["date"], s["slot"]) not in done:
                return True
        # 有该写还没写的日记
        return bool(self._pending_diary_days())

    def _pending_diary_days(self):
        """该写但还没写的日子（按时间正序，最多 MAX_DIARY_BACKFILL 天）"""
        now = datetime.datetime.now()
        today = now.date()
        floor = self._floor_date(now)          # 不给她"开始过日子"之前的日子补日记
        have = {e.get("date") for e in self.events(days=MAX_BACKFILL_DAYS + 2)}
        out = []
        for d in range(MAX_DIARY_BACKFILL, -1, -1):
            day = today - datetime.timedelta(days=d)
            if day < floor:
                continue
            ds = day.isoformat()
            if ds not in have or self.has_diary(ds):
                continue
            if day == today and (now.hour, now.minute) < (self.diary_hour, self.diary_min):
                continue        # 今天还没到写日记的点
            out.append(ds)
        return out[:MAX_DIARY_BACKFILL]

    # --- 跨进程锁（桌宠 / 其它入口可能同时开着） ---
    def _acquire_file_lock(self):
        """抢到返回 True，抢不到（别人正在补）返回 False。

        用"建文件"当锁 —— O_CREAT|O_EXCL 是原子的，Windows 上也管用。锁文件本身出任何问题
        都不该挡住正经功能，所以出错一律放行。
        """
        try:
            os.makedirs(LIFE_DIR, exist_ok=True)
            if os.path.exists(LOCK_PATH):
                try:
                    if time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE:
                        return False           # 另一个入口正在补算，让它算
                except OSError:
                    pass
                try:
                    os.remove(LOCK_PATH)       # 残留锁（上次崩了没删掉）
                except OSError:
                    pass
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False                       # 撞上了另一进程刚建的锁
        except OSError:
            return True                        # 锁机制本身出问题 → 别挡功能

    def _release_file_lock(self):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass

    def catch_up(self, force=False):
        """把落后补上。线程安全 + 节流。返回一个简短的结果字典。

        会调模型，慢（几秒到几十秒），必须放在后台线程里跑。
        """
        now = datetime.datetime.now()
        with self._lock:
            if not self.enabled:
                return {"ok": True, "skip": "已关闭"}
            if not force and now.timestamp() - self._last_run < MIN_INTERVAL:
                return {"ok": True, "skip": "刚补过"}
            # 桌宠和其它入口是两个进程，别两边一起给她补算
            if not self._acquire_file_lock():
                return {"ok": True, "skip": "另一个入口正在补算"}
            self._last_run = now.timestamp()
            try:
                return self._catch_up_locked(now)
            finally:
                self._release_file_lock()

    def _catch_up_locked(self, now):
        res = {"ok": True, "events": 0, "diary": 0, "calls": 0}

        # --- 0. 第一次跑：记下她"开始过日子"的那天（之后就不再往前追溯） ---
        st = self.state()
        if not st.get("started"):
            st["started"] = now.date().isoformat()
            # 标记这份生活属于哪套人设，换人设后据此判断该不该重置
            st["persona"] = self.persona
            self._save_state(st)

        # --- 1. 找出缺哪些片段 ---
        done = self._done_pairs()
        pending = [s for s in slots_between(self._begin(now), now)
                   if (s["date"], s["slot"]) not in done]
        if not pending:
            res["events"] = 0
            self._advance_state(now)
            res["diary"] = self._write_pending_diaries()
            self.last_result = res
            return res

        older, recent = [], pending
        if len(pending) > MAX_DETAIL_SLOTS:
            older, recent = pending[:-MAX_DETAIL_SLOTS], pending[-MAX_DETAIL_SLOTS:]

        # --- 2. 太久没来的那几天：一句概括 ---
        if older:
            s = self._gen_summary(older, self.events(days=MAX_BACKFILL_DAYS + 2))
            res["calls"] += 1
            if s is None:
                res["ok"] = False
                self.last_result = res
                return res
            self._append_events([s])
            res["events"] += 1

        # --- 3. 最近的部分：按天细写 ---
        by_day = {}
        for s in recent:
            by_day.setdefault(s["date"], []).append(s)
        today = now.date().isoformat()
        latest_mood, latest_doing = "", ""
        for ds in sorted(by_day):
            slots = by_day[ds]
            d = self._gen_day(ds, slots, self.events(days=MAX_BACKFILL_DAYS + 2))
            res["calls"] += 1
            if d is None:
                res["ok"] = False        # 不推进，下次再试这一段
                break
            self._append_events(d["events"])
            res["events"] += len(d["events"])
            if ds == today:
                latest_mood, latest_doing = d["mood"], d["doing"]
            if d["mood"]:
                st = self.state()
                st.setdefault("moods", {})[ds] = d["mood"]
                self._save_state(st)

        self._advance_state(now, doing=latest_doing, mood=latest_mood)
        res["diary"] = self._write_pending_diaries()
        self.last_result = res
        return res

    def _advance_state(self, now, doing="", mood=""):
        st = self.state()
        today = now.date().isoformat()
        st["last_run"] = _fmt(now)
        st["slot"] = slot_of(now)[0]
        if doing:
            st["doing"] = doing
            # 记下这个 doing 属于哪个时段 —— 读的时候才知道它是不是"此刻"
            st["doing_slot"] = slot_of(now)[0]
            st["doing_at"] = _fmt(now)
        # 心情按天取：前一天的情绪不能留到今天。没有当天的就宁可不写，
        # 别拿隔夜的糊弄（实测凌晨三点还在用白天那句"乱糟糟"）
        if mood:
            st["mood"] = mood
        else:
            st["mood"] = (st.get("moods") or {}).get(today, "")
        self._save_state(st)

    def _write_pending_diaries(self):
        n = 0
        for ds in self._pending_diary_days():
            if self.write_diary(ds):
                n += 1
        return n

    # --- 注入给对话的文本 ---
    def life_block(self, max_events=7):
        if not self.enabled:
            return ""
        now = datetime.datetime.now()
        name, asleep = slot_of(now)
        st = self.state()
        today = now.date().isoformat()
        lines = []
        lines.append(f"【你自己的生活】现在是 {now.strftime('%H:%M')}"
                     f"（{'深夜' if asleep else name}）"
                     + ("—— 这个点你本来该睡了。" if asleep else "。"))
        # 基本情况一直带着 —— 这样她第一次开口就知道自己在哪上学、
        # 室友叫什么，不用等第一次补算完，也不会前后矛盾。
        where = self._world_block()
        if where:
            lines.append("你的基本情况：")
            lines.extend(where.splitlines())

        # 「此刻在干嘛」必须真的是此刻。23:00~07:00 不生成新片段，state 里的 doing 会一直
        # 卡在傍晚 / 晚上那一档 —— 实测凌晨三点她还在"吃西瓜"，就是把几小时前的事当成了现在。
        # 所以只有时段对得上才说"此刻"，否则一律改成过去时
        doing = st.get("doing") or ""
        if doing:
            when = st.get("doing_slot") or ""
            if when and when == name:
                lines.append(f"你此刻：{_strip_self(doing)}")
            elif when:
                lines.append(f"你{when}的时候在做：{_strip_self(doing)}"
                             f"（那是{when}的事，不是现在）")
            else:
                # 老数据没有 doing_slot，只能含糊一点，但同样不能说成"此刻"
                lines.append(f"你早些时候在做：{_strip_self(doing)}（不是现在）")

        # 心情只取当天的 —— 隔夜的情绪会让她说出莫名其妙的话
        mood = (st.get("moods") or {}).get(today) or ""
        if mood:
            lines.append(f"你的心情：{mood}")
        evs = [e for e in self.events(days=2) if e.get("date") == today]
        if evs:
            lines.append("你今天自己经历的事（都是真的，可以说）。注意：这些都是今天"
                         "早些时候的事，已经过去一段时间了——提到时说『中午那会儿』"
                         "『下午的时候』，绝不要说『刚』『正在』：")
            for e in evs[:max_events]:
                slot = e.get("slot") or ""
                gap = _slot_gap_text(slot, now)
                suffix = f"（{gap}）" if gap else ""
                lines.append(f"- {slot}{suffix}：{e.get('text')}")
        if len(lines) <= 1:
            return ""
        return "\n".join(lines)

    def diary_block(self, max_chars=200):
        ds = self.list_diaries()
        if not ds:
            return ""
        latest = ds[-1]
        if latest == datetime.date.today().isoformat():
            return ""            # 今天的日记还是热乎的，不必再塞一遍
        body = self.read_diary(latest)
        if not body:
            return ""
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        return f"【你的日记】你最近一篇日记（{latest}）写的是：\n{body}"

    # --- 自知：她知不知道"我会写日记 / 会发朋友圈" ---
    def self_aware_block(self):
        """告诉她"你自己会写日记、会发朋友圈"这两件事。

        不声明的话她在聊天里永远提不起这两件事：写日记走 _diary_prompt 那套独立提示词、
        朋友圈走 moments.py，两条路都不经过聊天的 system prompt，人设里也没写。

        这里只声明"你会做这两件事"，不塞日记正文（正文归 diary_block）。日记是她的私人物品，
        不整篇给人看。
        """
        if not self.enabled:
            return ""
        return ("【你自己会做的事（真的，你自己清楚）】\n"
                "- 你每天睡前会自己写一篇日记，是写给自己看的，不是写给他的。\n"
                "- 你偶尔会随手发条朋友圈，配图或者不配，发的都是你当天的小事。\n"
                "- 这两样都是你自己的事，没必要主动汇报；他问起来，可以挑你想说的说两句。\n"
                "- 但他要你把日记整篇拿给他看，你不给 —— 那是你自己看的东西，"
                "直接怼回去或者岔开（“想得美”“我自己看的”）。")

    def note_moment(self, text, imgs=None):
        """把她刚发的朋友圈追记成一条自己的经历。

        以前只写进 moments.json，life 这边一条记录都没有 —— 她发完自己就忘了，第二天聊天提
        不起来（life_block 只看 events）。

        slot 固定用"朋友圈"，**不能用"晚上"这类时段名**：_done_pairs() 的去重键是 (date, slot)，
        撞上会把当天那个时段的正常经历补算挡掉。未知 slot 在 _slot_gap_text 里返回空。
        """
        text = (text or "").strip()
        if not text:
            return
        try:
            now = datetime.datetime.now()
            rec = {"date": now.date().isoformat(), "slot": "朋友圈", "text": text}
            if imgs:
                n = len(imgs) if isinstance(imgs, (list, tuple)) else 1
                if n > 0:
                    rec["text"] = text + "（配了 %d 张图）" % n
            self._append_events([rec])
        except Exception:
            pass

    # --- 维护 ---
    def reset(self, keep_world=True):
        """让她重新开始（清掉经历和日记，保留世界设定）"""
        for p in (self.events_path, self.state_path):
            try:
                os.remove(p)
            except OSError:
                pass
        for p in self.list_diaries():
            try:
                os.remove(self.diary_path(p))
            except OSError:
                pass
        try:
            for n in os.listdir(self.home):
                if re.match(r"^chat_\d{4}-\d{2}-\d{2}\.jsonl$", n):
                    os.remove(os.path.join(self.home, n))
        except OSError:
            pass

    def stats(self):
        evs = self.events(days=MAX_BACKFILL_DAYS + 2)
        return {"世界设定": "已填" if self.world_ready() else "还是空的",
                "经历条数": len(evs),
                "日记篇数": len(self.list_diaries()),
                "此刻": (self.state().get("doing") or "—"),
                "需要补算": self.needs_catch_up()}
