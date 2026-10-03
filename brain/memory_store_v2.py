# -*- coding: utf-8 -*-
"""进阶记忆库 v2.1：向量检索 + API 智能提取 + 规则降级

索引是单个 vectors.npy 矩阵 + ids.json（旧格式"每条记忆一个 .npy"首次加载时自动
迁移）。索引加载后常驻内存，检索是纯矩阵运算、不逐条读盘，机械硬盘也不怕记性变大。

嵌入走云端：云服务器连不上 HuggingFace，本地 fastembed 每次初始化都要去 HF 下模型、
必然超时 —— 结果 11 条记忆只有 2 条有向量，retrieve() 永远走"最近几条"的降级分支，
等于没有语义检索。现在走 config.json 里同一个 api_base 的 /embeddings。

硅基流动上**没有** bge-small-zh-v1.5（报 Model does not exist），可用的中文模型是
BAAI/bge-m3 与 BAAI/bge-large-zh-v1.5，均为 1024 维。换模型会让旧向量维度对不上，
所以用 embed_meta.json 记下模型名，发现不一致就整体重建索引。

- retrieve(query, top_k)：按语义相似度只注入最相关的记忆
- 提取：优先 API（OpenAI 兼容），失败 / 无 key 自动降级规则提取
"""
import json, os, re, time
import threading
import numpy as np
import httpx

# 云端嵌入模型（1024 维）。改这里会自动触发索引重建，不用手工删向量文件
EMBED_API_MODEL = os.environ.get("EMBED_API_MODEL", "BAAI/bge-m3")
# 本地兜底模型（要能连 HuggingFace 才用得上）
LOCAL_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

_embedder = None
_embed_dead = False     # 本进程内已确认 embedding 不可用，别再每轮白等


# ============================================================================
# 她说的话要不要进记忆库
# ============================================================================
# 以前只抽用户的话，她说的承诺/计划/约定一律不记 —— 于是她说过"明天要去望江楼
# 拍银杏"，转头自己检索不到；上下文一被裁掉，那条就彻底没了。
#
# 但反过来全抽也不行：她会凭空造事。实测她主动搭话时说过"下午那个高铁，你去接我
# 啊"，历史里从没这回事，记忆库里也没有 —— 是她编的。这种话存进去更糟：下次她
# 会"记得"自己说过高铁。
#
# 所以按语气分开：承诺（"我下午去接你"）要记，假设（"要是无聊高铁三小时过来也行"）
# 不记。判据不是词表穷举，是句子里有没有把事情说成已经定下的安排。

# 假设/条件句的标志。这些词一出现，整句就不进记忆
_HYPOTHETICAL = (
    "要是", "如果", "假如", "万一", "万一", "要不", "不如", "算了", "随便",
    "要不这样", "有可能", "大概", "也许", "说不定", "估计", "改天", "哪天",
    "有机会", "有空", "没事的话", "的话就行", "也行", "就好", "凑合",
)

# 承诺句的标志：她把某件事说成了定下的安排
_COMMIT = (
    "我去接", "我去接你", "来接你", "我去找你", "我请", "我请你", "我带",
    "我给你", "我帮你", "我订", "我买", "我定了", "我约了", "我约了人",
    "说好了", "说定", "定了", "约定", "答应", "保证", "一定", "肯定",
    "别忘", "记得", "等我", "我在", "我上午", "我下午", "我晚上", "我明天",
    "我今天", "我这就去", "我就来", "我这边", "我先", "我待会",
    # 省略主语的时间安排："下午那个高铁，你去接我啊" —— 主动搭话里
    # 主语常常省掉，句子里看不出是谁的安排，这类最容易混进幻觉
    "上午", "下午", "晚上", "明天", "今天", "早上", "中午", "傍晚",
)

# 明显是玩笑/敷衍的说法，不进记忆
_JOKING = (
    "开玩笑", "逗你", "骗你的", "假的", "吓你的", "哈哈", "233",
    "你猜", "开玩笑的",
)

# 省略主语的时间安排：光有时间词不够，必须配一个具体动作，且不能是纯状态描述。
# "我今天状态不错"有时间词但没有动作，不该记；"今天学习了Python"有动作，记。
# 纯状态描述。有时间词但整句在讲状态/感受，不是安排。
# 注意用短语而不是单词："我明天还得去望江楼拍银杏呢，困死了"里有"困"，
# 但那是修饰前面的安排，单词匹配会把真承诺误杀。
_STATE_ONLY = (
    "状态不错", "状态不好", "心情不错", "心情不好", "心情有点",
    "感觉不错", "感觉不好", "有点困", "有点累", "有点烦", "有点难过",
    "好困", "很困", "特别困", "睡不着", "睡不好", "还行", "挺好", "还不错",
)

# 讲已经发生/过去的事，不是将来的安排
_PAST_MARK = (
    "刚", "刚才", "刚刚", "已经", "早就", "完了", "过了", "之前", "昨晚",
    "昨儿", "上午还", "下午还",
)

# 天气/状态播报 —— 固定套路（"你那边17度，出门记得穿外套"），不是安排
_FORECAST = ("出门记得", "记得穿", "带伞", "天气", "气温", "度", "下雨", "降温")


def classify_her_speech(text):
    """判断她说的这句话该不该进记忆库。

    返回 (should_save, reason)：
      ("promise", ...)      承诺/约定，要记
      ("hypothetical", ...) 假设句，不记
      ("joking", ...)       玩笑，不记
      ("forecast", ...)     天气播报，固定套路不记
      ("chitchat", ...)     闲聊，不记

    为什么不用单个词判断：「我明天有点事」是安排，「我明天要是不去看书就好了」
    同样是"我明天"开头，但后者是假设。得看整句有没有转折和条件。

    最难的一条是省略主语的时间安排。主动搭话里主语常常省掉，句面上看不出是谁
    的安排，这类恰好最容易是编的。判据是光有时间词不够 —— 必须还出现一个具体
    动作（接/买/带/去/见…），纯状态播报和已经发生的事都不算。
    """
    t = (text or "").strip()
    if len(t) < 4:
        return False, "chitchat"

    for w in _JOKING:
        if w in t:
            return False, "joking"

    # 假设句：出现条件标志 → 不记（先判这个，它优先级高于承诺标志：
    # "要是我明天下午去，你就别等了"有"要是"也命中承诺）
    for w in _HYPOTHETICAL:
        if w in t:
            return False, "hypothetical"

    # 天气播报固定套路
    if any(w in t for w in _FORECAST):
        return False, "forecast"

    has_time = any(w in t for w in ("上午", "下午", "晚上", "明天", "今天",
                                   "早上", "中午", "傍晚", "后天", "周"))
    # 具体动作：接/送/买/带/见/去某地/约人，没有这个就只是"提到了时间"
    has_action = any(w in t for w in (
        "接", "送", "买", "带", "见", "见你", "去找", "来", "去", "约",
        "订", "吃", "喝", "拿", "取", "寄", "拍", "提醒", "等你", "回来",
    ))

    # 讲已经发生的事，不是安排
    if any(w in t for w in _PAST_MARK):
        return False, "chitchat"

    # 纯状态描述：有时间词但整句在讲状态/感受，不是安排。
    # 放在第一人称判定之前 —— "我今天状态不错"含"我"，否则会被当成承诺放行。
    if any(w in t for w in _STATE_ONLY):
        return False, "chitchat"

    # 主语明确的承诺：她说了"我" + 动作，或明确的第一人称时间安排
    first_person = "我" in t
    if first_person and any(w in t for w in _COMMIT):
        return True, "promise"

    # 省略主语的时间安排：光有时间词不够，必须配一个具体动作
    if has_time and has_action:
        return True, "promise"

    return False, "chitchat"


# ============================================================================
# 注入时的权重：过期的、存疑的，往后排
# ============================================================================
# 记忆库是只增不减的，库里躺着「25号她坐车去找他」（写于 9-22，日期早已过）这种。
# 检索只看语义相似度，不看日期，于是她随时可能把三周前的事当成"这几天要办"说出来。
# 这里在 render 的时候按日期和来源打折 —— 不删数据（删了找不回来），只是不优先
# 给她看，实在低于阈值就不给。

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13,
           "十四": 14, "十五": 15, "十六": 16, "十七": 17, "十八": 18,
           "十九": 19, "二十": 20, "廿": 2, "两": 2}

# "25号""25 号""25日" → 25
_RE_DAY = re.compile(r"(\d{1,2})\s*[号日]")
# "9月22""9 月 22 号" → (9, 22)
_RE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]?")
# "明天""后天""今天" → 偏移天数（今天=0）
_RE_RELDAY = {"今天": 0, "今晚": 0, "明天": 1, "明晚": 1, "后天": 2,
              "大后天": 3, "昨天": -1, "前天": -2}


def _due_date(text, base=None, written=None):
    """从记忆文本里解析"这件事的截止日"，解析不出来返回 None。

    只认三种写法：9月22号 / 25号 / 明天。不做"三天后""下周五"这类模糊推算 ——
    猜错日期比不猜更糟（她会说错日期）。

    base    判定"过期"的当前时间，默认现在
    written 这条记忆写下来的时间。裸日号（"25号"）靠它定月份：写于 9-22 的
            "25号她坐车去找他"说的是 9-25，不是 10-25。这个锚定不做就没法判 ——
            只看当前日期，10-03 解析「25号」永远得到未来的 10-25，那条过期 8 天的
            记忆就永远降不了权。
    """
    import datetime
    now = base or datetime.datetime.now()
    anchor = written or now

    m = _RE_MD.search(text)
    if m:
        mo, da = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= da <= 31):
            return None
        # 带月份的是绝对日期，但月份已过说明是去年的事（"9月25号"写于 9-22，
        # 到 10 月再看就该算过期，而不是再等到 2027）
        try:
            due = datetime.datetime(anchor.year, mo, da, 23, 59)
        except ValueError:
            return None
        return due

    m = _RE_DAY.search(text)
    if m:
        da = int(m.group(1))
        if not 1 <= da <= 31:
            return None
        # 裸日号：以**写入时间**所在月份为基准找该日号。往后顺延会让过期的事
        # "活"到下个月（9-22 写的"25号"到 10-03 就成了 10-25，永远不过期）。
        y, mo = anchor.year, anchor.month
        for _ in range(3):          # 本月 + 往前 2 个月，够用了
            try:
                due = datetime.datetime(y, mo, da, 23, 59)
            except ValueError:
                due = None
            if due is not None and (due <= anchor
                                    or (anchor - due).days <= 45):
                return due
            mo -= 1
            if mo < 1:
                mo = 12
                y -= 1
        return None

    for w, off in _RE_RELDAY.items():
        if w in text:
            return anchor + datetime.timedelta(days=off)
    return None


_AGENDA_MISSED = None
_AGENDA_MTIME = 0.0


def _agenda_missed_texts():
    """从 agenda.json 取"已被标记为错过"的约定原文集合。

    为什么借 agenda 的判定而不在这里自己解析日期：agenda 每天跑一次 settle 把过期
    条目盖成 missed，它的日期窗口是唯一权威。同一件事存在两份（memory.json 和
    agenda.json），这边再解析一遍日期必然漂移 —— 实测就漂了：agenda 里 9 条已标
    missed，memory.json 里同样的内容还是满权重，她照样当"即将发生"提。

    只读不写。agenda 那边有完整实现（settle/auto_done/过期不复活），不在这里重造。
    """
    global _AGENDA_MISSED, _AGENDA_MTIME
    import json as _json
    # agenda.json 与 memory/ 都挂在 data/ 下。这里不 import paths（那段 import 在
    # 文件后段，本函数位置在它之前），从记忆库目录往上退一层就是 data/。
    try:
        d = os.path.dirname(os.path.abspath(configured_mem_dir()))
        p = os.path.join(d, "agenda.json")
    except Exception:
        return _AGENDA_MISSED or set()
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return set()
    if _AGENDA_MISSED is not None and mt == _AGENDA_MTIME:
        return _AGENDA_MISSED
    try:
        with open(p, encoding="utf-8") as f:
            d = _json.load(f)
        items = d.get("items") or (d if isinstance(d, list) else [])
    except Exception:
        return _AGENDA_MISSED or set()
    out = set()
    for it in items:
        st = (it.get("status") or "").strip()
        # 没有 status 的老条目现算一次，跟 agenda._status 同一判据
        if not st:
            try:
                import datetime as _dt
                dd = _dt.date.fromisoformat(it.get("date") or "")
                st = "missed" if dd < _dt.date.today() else ""
            except ValueError:
                st = ""
        if st == "missed":
            t = (it.get("text") or "").strip()
            if t:
                out.add(t)
    _AGENDA_MISSED, _AGENDA_MTIME = out, mt
    return out


def _weight(item):
    """这条记忆注入时的权重系数。会被 render 乘到相似度上，压到阈值下就不注入。

    三档降权：
      - agenda 已判 missed 的约定 ×0.35（权威来源，见 _agenda_missed_texts）
      - 自己解析出日期且已过期 ×0.35（agenda 没收录的漏网之鱼）
      - src=proactive ×0.8（主动搭话自己冒出来的承诺，多半是编的）

    只降权不删：删了找不回来，而"错过的事"本身也是有用的记忆（她会跟用户复盘
    "说好今天去看书的，结果你也没去"）。
    """
    if not item:
        return 1.0
    w = 1.0
    text = (item.get("text") or "").strip()

    if text and text in _agenda_missed_texts():
        return 0.35        # 权威判定，直接给过期档，不用再解析日期

    written = None
    if item.get("time"):
        try:
            import datetime
            written = datetime.datetime.strptime(item["time"], "%Y-%m-%d %H:%M")
        except ValueError:
            written = None

    due = _due_date(text, written=written)
    if due is not None:
        import datetime
        overdue = datetime.datetime.now() - due
        if overdue.days >= 7:
            w *= 0.35
        elif overdue.days >= 1:
            w *= 0.75

    if (item.get("src") or "") == "proactive":
        w *= 0.8

    return w


def _cfg_api():
    """拿 config.json 里的 api_base / api_key（跟聊天用的是同一份凭据）"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            c = json.load(f)
        base = str(c.get("api_base") or "").rstrip("/")
        key = str(c.get("api_key") or "")
        if base and key:
            return base, key
    except Exception:
        pass
    return "", ""


def _api_embed(texts, timeout=30):
    """调 OpenAI 兼容的 /embeddings。失败返回 None，让调用方降级"""
    base, key = _cfg_api()
    if not base:
        return None
    r = httpx.post(base + "/embeddings",
                   headers={"Authorization": "Bearer " + key,
                            "Content-Type": "application/json"},
                   json={"model": EMBED_API_MODEL, "input": texts},
                   timeout=timeout)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or []
    if len(data) != len(texts):
        return None
    data.sort(key=lambda x: x.get("index", 0))
    vecs = []
    for d in data:
        v = np.asarray(d.get("embedding") or [], dtype=np.float32)
        if v.size == 0:
            return None
        vecs.append(v)
    return vecs

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import paths
    CONFIG_PATH = paths.CONFIG_PATH
    FALLBACK_MEM_DIR = paths.MEMORY_DIR      # data/memory
except Exception:
    CONFIG_PATH = os.path.join(_HERE, "config", "config.json")
    FALLBACK_MEM_DIR = os.path.join(_HERE, "data", "memory")


def get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding
        _embedder = TextEmbedding(model_name=LOCAL_EMBED_MODEL)
    return _embedder


def embed_texts(texts):
    """把文本变成向量。云端优先，失败降级本地 fastembed，再失败返回 []。

    返回 [] 时调用方会走"最近几条"的降级检索，不会崩。_embed_dead 是进程内开关：
    确认挂了之后就不再每轮重试，否则每句话都要白等一次连不上的请求。
    """
    global _embed_dead
    if not texts:
        return []
    if _embed_dead:
        return []
    # 1) 云端 API（云上服务器下不动 HF，这条是主力）
    try:
        vecs = _api_embed(texts)
        if vecs:
            return vecs
    except Exception as e:
        print(f"[记忆] 云端 embedding 失败，降级本地：{type(e).__name__} {e}", flush=True)
    # 2) 本地 fastembed（要能连 HuggingFace）
    try:
        vecs = list(get_embedder().embed(texts))
        return [np.asarray(v, dtype=np.float32) for v in vecs]
    except Exception as e:
        print(f"[记忆] 本地 embedding 也不可用，本次放弃向量检索："
              f"{type(e).__name__} {e}", flush=True)
        _embed_dead = True
        return []


def cosine_sim(a, b):
    a = a / (np.linalg.norm(a) + 1e-9)
    b = b / (np.linalg.norm(b) + 1e-9)
    return float(a @ b)


def configured_mem_dir():
    """记忆目录：读 config.json 的 memory_dir，没有则用脚本同级的 memory_v2"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            p = json.load(f).get("memory_dir")
        if p:
            return p
    except Exception:
        pass
    return FALLBACK_MEM_DIR


def get_default_store():
    """按 config.json 打开默认记忆库"""
    d = configured_mem_dir()
    return MemoryStore(os.path.join(d, "memory.json"))


def _synchronized(fn):
    """给实例方法套上它自己那把 RLock（可重入，嵌套调用不会自锁）。

    只用来包"碰内存状态或落盘"的方法；网络调用（embed_texts）不进锁，
    见 MemoryStore.add 的注释。
    """
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper


class MemoryStore:
    """一个记忆库实例。

    实例自带一把 RLock：**记忆写入跑在后台线程里**（brain._extract_bg），而请求线程
    同时可能在 retrieve()/render() 读同一份索引 —— 两边都会碰 self.data["items"]、
    self._ids/_rows 和 vectors.npy / memory.json。没有锁的话并发写盘是可达的，
    后果是记忆库或向量索引损坏（而且损坏是静默的，下次启动才发现）。

    纪律：**网络调用（embed_texts）不放在锁里** —— 否则一次 embedding 超时会把
    检索一起堵住。锁只保护内存状态与落盘。
    """

    def __init__(self, path, embed_dir=None):
        self._lock = threading.RLock()      # 先于 _load()：读盘也要在锁里
        self.path = path
        self.embed_dir = embed_dir or os.path.dirname(os.path.abspath(path))
        self.data = {"items": [], "meta": {"created": time.strftime("%Y-%m-%d %H:%M")}}
        self._load()
        # --- 向量索引（懒加载，加载一次后进内存） ---
        self.vec_path = os.path.join(self.embed_dir, "vectors.npy")
        self.ids_path = os.path.join(self.embed_dir, "ids.json")
        self.meta_path = os.path.join(self.embed_dir, "embed_meta.json")
        self._mat = None      # (N, dim) float32，行序与 _ids 对齐
        self._ids = []        # 与矩阵行一一对应的记忆 id
        self._rows = []       # 与 _ids 一一对应的向量
        self._imap = {}       # id -> 行号
        self._dim = 0         # 实际向量维度，由落盘的矩阵决定
        self._index_loaded = False
        self.last_hits = []          # 上一次 render() 命中的 (相似度, 文本)，见 render

    # --- 持久化 ---
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"items": [], "meta": {"created": time.strftime("%Y-%m-%d %H:%M")}}
        # 兼容 v1 格式：facts/events -> items
        if "items" not in self.data:
            items = []
            for k, v in self.data.get("facts", {}).items():
                items.append({"id": k, "type": "fact", "text": f"{k}：{v['value']}", "time": v.get("time", "")})
            for e in self.data.get("events", []):
                items.append({"id": f"e{len(items)}", "type": "event", "text": e.get("text", ""), "time": e.get("time", "")})
            self.data = {"items": items, "meta": {"created": time.strftime("%Y-%m-%d %H:%M")}}

    @_synchronized
    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # --- 向量索引 ---
    @_synchronized
    def _load_index(self):
        """把向量索引读进内存。只在第一次调用时产生磁盘 IO，之后检索零读盘"""
        if self._index_loaded:
            return
        ids, rows = [], []
        # 换过 embedding 模型（维度会变）就别读旧矩阵了，直接重建
        if self._meta_matches() and os.path.isfile(self.vec_path) \
                and os.path.isfile(self.ids_path):
            try:
                ids = json.load(open(self.ids_path, encoding="utf-8"))
                mat = np.load(self.vec_path)
                if mat.ndim == 2 and len(ids) == mat.shape[0]:
                    rows = [mat[i] for i in range(mat.shape[0])]
                    self._dim = mat.shape[1]
                else:
                    ids, rows = [], []
            except Exception:
                ids, rows = [], []
        if not rows:
            ids, rows = self._migrate_legacy()
        self._ids, self._rows = ids, rows
        self._imap = {i: k for k, i in enumerate(ids)}
        self._mat = None
        self._index_loaded = True
        self._backfill()

    def _meta_matches(self):
        """索引是不是当前这个 embedding 模型建的。换模型 → 返回 False → 重建"""
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                m = json.load(f)
            return m.get("model") == EMBED_API_MODEL
        except Exception:
            return False

    def _write_meta(self, dim):
        try:
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump({"model": EMBED_API_MODEL, "dim": int(dim)},
                          f, ensure_ascii=False)
        except Exception as e:
            print(f"[记忆] 写 embed_meta.json 失败：{e}", flush=True)

    def _reset_index(self):
        """清空内存索引（维度变了或模型换了时用）"""
        self._ids, self._rows = [], []
        self._imap = {}
        self._mat = None
        self._dim = 0

    def _migrate_legacy(self):
        """兼容 v2.0：emb 目录下每条记忆一个 .npy。读过一次后就不再用了"""
        ids, rows = [], []
        legacy = self.embed_dir if os.path.basename(self.embed_dir) == "emb" else os.path.join(self.embed_dir, "emb")
        if not os.path.isdir(legacy):
            return ids, rows
        for it in self.data["items"]:
            p = os.path.join(legacy, it["id"] + ".npy")
            if os.path.exists(p):
                try:
                    rows.append(np.load(p).astype(np.float32))
                    ids.append(it["id"])
                except Exception:
                    pass
        if ids:
            print(f"[记忆] 已从旧格式迁移 {len(ids)} 条向量", flush=True)
            self._ids, self._rows = ids, rows
            self._save_index()
        return ids, rows

    def _backfill(self):
        """给还没向量的记忆补算（比如手工往 memory.json 里加过条目）"""
        missing = [it for it in self.data["items"] if it["id"] not in self._imap]
        if not missing:
            return
        vecs = embed_texts([it["text"] for it in missing])
        if not vecs:
            return                                  # embedding 不可用，下次再说
        # 维度跟已有向量对不上（比如云端挂了降级到本地小模型）→ 整体按新维度重算
        if self._rows and vecs[0].shape[0] != self._rows[0].shape[0]:
            print(f"[记忆] 向量维度 {self._rows[0].shape[0]} → {vecs[0].shape[0]}，"
                  f"重建索引", flush=True)
            self._reset_index()
            missing = list(self.data["items"])
            vecs = embed_texts([it["text"] for it in missing])
            if not vecs:
                return
        for it, v in zip(missing, vecs):
            if it["id"] in self._imap:
                continue
            self._imap[it["id"]] = len(self._rows)
            self._ids.append(it["id"])
            self._rows.append(v)
        self._mat = None
        self._dim = self._rows[0].shape[0] if self._rows else 0
        self._save_index()

    def _save_index(self):
        os.makedirs(self.embed_dir, exist_ok=True)
        if self._rows:
            np.save(self.vec_path, np.vstack(self._rows).astype(np.float32))
            self._dim = self._rows[0].shape[0]
        else:
            np.save(self.vec_path, np.zeros((0, self._dim or 1), dtype=np.float32))
        with open(self.ids_path, "w", encoding="utf-8") as f:
            json.dump(self._ids, f, ensure_ascii=False)
        if self._dim:
            self._write_meta(self._dim)

    def _matrix(self):
        if self._mat is None:
            self._mat = (np.vstack(self._rows).astype(np.float32)
                         if self._rows else np.zeros((0, self._dim or 1),
                                                     dtype=np.float32))
        return self._mat

    # --- 写入 ---
    def add(self, text, mtype="event", src=""):
        """添加一条记忆（自动向量化）。

        本方法是唯一会从**别的线程**跑的写入口（brain 里记忆抽取是后台线程），
        所以它不用 _synchronized 整包：embed_texts 是网络调用，放在锁外，
        免得一次 embedding 超时把请求线程的检索一起堵住。

        src 记这条记忆的来源：chat = 对话里说的（可信），proactive = 主动搭话
        自己冒出来的（大概率是编的，检索时降权）。空 = 用户那边抽的。
        """
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            for it in self.data["items"]:
                if it["text"] == text:
                    return
        vs = embed_texts([text])                    # 网络调用：不进锁
        with self._lock:
            # 等 embedding 这一会儿，别的线程可能已经把同一条写进去了，再查一次
            for it in self.data["items"]:
                if it["text"] == text:
                    return
            item = {"id": f"m{int(time.time()*1000)}{len(self.data['items'])}",
                    "type": mtype, "text": text,
                    "time": time.strftime("%Y-%m-%d %H:%M")}
            if src:
                item["src"] = src
            self.data["items"].append(item)
            self.save()
            # 向量化并追加到内存索引
            self._load_index()
            if item["id"] in self._imap:
                return
            if not vs:
                return                              # 向量化不了也先把记忆存下来
            v = vs[0]
            if self._rows and v.shape[0] != self._rows[0].shape[0]:
                self._reset_index()                 # 维度变了，交给 _backfill 全量重算
                self._backfill()
                return
            self._imap[item["id"]] = len(self._rows)
            self._ids.append(item["id"])
            self._rows.append(v)
            self._mat = None
            self._dim = v.shape[0]
            self._save_index()

    @_synchronized
    def delete(self, item_id):
        self.data["items"] = [i for i in self.data["items"] if i["id"] != item_id]
        self.save()
        self._load_index()
        if item_id in self._imap:
            self._imap.pop(item_id)
            keep = [k for k, i in enumerate(self._ids) if i != item_id]
            self._rows = [self._rows[k] for k in keep]
            self._ids = [self._ids[k] for k in keep]
            self._imap = {i: k for k, i in enumerate(self._ids)}
            self._mat = None
            self._save_index()

    @_synchronized
    def list_items(self):
        return list(self.data["items"])

    # --- 向量检索 ---
    @_synchronized
    def retrieve(self, query, top_k=5, min_score=0.25):
        """按语义相似度返回 Top-K 记忆文本。索引在内存里，不逐条读盘

        整段在锁里（含 embed_texts）：检索走的是请求线程，而对话本身已被
        server 的全局锁串行化，所以这里不会真的和别的检索并发 ——
        进锁是为了挡住后台记忆写入线程正在改 _ids/_rows/_mat。
        """
        if not self.data["items"]:
            return []
        self._load_index()
        M = self._matrix()
        if M.shape[0] == 0:
            return []
        vs = embed_texts([query])
        # 拿不到向量、或维度跟索引对不上（换过模型还没重建）→ 降级：最近几条
        if not vs or vs[0].shape[0] != M.shape[1]:
            # 返回值形状必须和正常路径一致（(相似度, 文本) 元组）。以前这里返回
            # 纯文本列表，render 按元组解包直接 ValueError —— 而这条路恰好是
            # embedding 挂掉时才走的，等于"服务最需要降级的时候崩在降级里"。
            # 给 0.30：非零（降权逻辑还能作用），但低于 render 的 0.40 阈值，
            # 正好表示"这几条只是最近的说得上话，不是真的相关"。
            return [(0.30, i["text"]) for i in self.data["items"][-top_k:]]
        qv = vs[0]
        q = qv / (np.linalg.norm(qv) + 1e-9)
        Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
        sims = Mn @ q
        order = np.argsort(-sims)
        text_of = {it["id"]: it["text"] for it in self.data["items"]}
        out = []
        for idx in order[:top_k]:
            s = float(sims[idx])
            if s < min_score:
                break
            t = text_of.get(self._ids[idx])
            if t:
                out.append((s, t))
        return out

    @_synchronized
    def render(self, query=None, top_k=5, max_items=20, min_score=0.40):
        """注入块：有 query 用检索，无 query 用最近 max_items 条。

        min_score 取 0.40 而不是 0.25：0.25 太松，随便一句闲聊都能拉出 5 条"沾边"的旧记忆
        塞进上下文，她的话题和情绪被旧事带跑。宁可少喂，不可喂错 —— 记忆是调味，不是主菜。

        检索到的条目还要过一道 _weight：
        - 带日期且已过期的事件降权（"25号她坐车去找他"过期 8 天了还躺在库里，
          她随时可能当成即将发生提起）
        - 主动搭话自己冒出来的承诺降权（src=proactive，那多半是编的）
        降权后低于阈值的直接不注入，但不删 —— 删了找不回来。
        """
        if query:
            hits = self.retrieve(query, top_k=top_k, min_score=0.0)
            self.last_hits = list(hits)      # trace 落盘用原始相似度
            rows = []
            for s, t in hits:
                w = _weight(self._find_item(t))
                if w <= 0:
                    continue
                s2 = s * w
                if s2 >= min_score:
                    rows.append((s2, t))
            rows.sort(key=lambda x: -x[0])
            if rows:
                return ("你记得他的事（这些是真的，聊天时自然地用上）：\n"
                        + "；".join(t for _, t in rows[:top_k]))
        items = self.data["items"][-max_items:]
        rows = []
        for i in items:
            w = _weight(i)
            if w <= 0:
                continue
            rows.append((i["text"], w))
        if not rows:
            return ""
        # 过期/存疑的那批放最后，正常的那批在前 —— 模型对列表末尾更敏感，
        # 放末尾等于降权，和截断思路一致。
        # 用反向排序（reverse=True）：权重低的在后面。写成升序会把过期的顶到最前
        # —— 实测「今天学习了Python一天」直接排到第一条了。
        rows.sort(key=lambda x: x[1], reverse=True)
        return ("你记得他的事（这些是真的，聊天时自然地用上）：\n"
                + "；".join(t for t, _w in rows))

    def _find_item(self, text):
        for it in self.data["items"]:
            if it["text"] == text:
                return it
        return None

    @_synchronized
    def stats(self):
        """记忆库概况，排查用"""
        self._load_index()
        total = sum(1 for _ in self.data["items"])
        vec_mb = (self._matrix().nbytes / 1024 / 1024) if self._matrix().shape[0] else 0.0
        try:
            json_kb = os.path.getsize(self.path) / 1024
        except OSError:
            json_kb = 0
        return {"目录": self.embed_dir, "记忆条数": total, "已向量化": len(self._ids),
                "向量大小": f"{vec_mb:.2f} MB", "memory.json": f"{json_kb:.1f} KB"}

    # --- API 智能提取 ---
    @staticmethod
    def extract_with_api(user_text, api_config=None, timeout=25):
        """用 OpenAI 兼容 API 提取记忆，返回 (facts, events)"""
        if not api_config or not api_config.get("api_key"):
            return None
        prompt = (
            "你是记忆助手。从下面这句用户的话里，提取值得长期或近期记住的信息。\n"
            "规则：\n"
            "1. 长期事实（宠物/家人/喜好/职业/习惯/朋友）放进 facts\n"
            "2. 近期事件或待办（考试、聚会、deadline、计划、身体不适、重要安排）放进 events\n"
            "3. 只提取明确的、有用的信息；纯情绪发泄（如'好累''烦'）不要记\n"
            "输出严格 JSON：{\"facts\": [{\"key\": \"简短类别\", \"value\": \"具体内容\"}], \"events\": [\"一句话事件或待办\"]}\n"
            "没有可提取的就输出 {\"facts\": [], \"events\": []}。\n"
            f"用户的话：{user_text}"
        )
        try:
            r = httpx.post(
                api_config["api_base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_config["api_key"]},
                json={"model": api_config.get("model", "deepseek-chat"),
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 300},
                timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
            data = json.loads(content)
            facts = [(f.get("key", ""), f.get("value", "")) for f in data.get("facts", []) if f.get("value")]
            events = [e for e in data.get("events", []) if e]
            return facts, events
        except Exception:
            return None

    # --- 规则提取（降级） ---
    @staticmethod
    def extract_rule(user_text):
        out = []
        t = user_text.strip()
        if not t:
            return out
        m = re.search(r"(?:养了|有|养)(?:一?只|条|个)?(猫|狗|仓鼠|兔子|乌龟|鸟|鱼|小动物)(?:子)?(?:名字叫|叫|起名|取名叫)?([\u4e00-\u9fa5A-Za-z0-9]{1,4})", t)
        if m:
            out.append(("宠物", f"{m.group(1)}叫{m.group(2)}"))
        m = re.search(r"(?:我|我特别|我最)(喜欢|爱吃|讨厌|烦|不爱吃)(.+?)(?:。|！|$)", t)
        if m:
            key = "喜欢" if m.group(1) in ("喜欢", "爱吃") else "讨厌"
            val = m.group(2).strip("，,。！! ")
            if len(val) <= 12 and val:
                out.append((key, val))
        m = re.search(r"我是(?:一个?)?(.{2,10}?(?:开发|程序员|学生|设计|运营|老师|医生|会计|销售|产品|工程|码农))", t)
        if m:
            out.append(("职业", m.group(1)))
        m = re.search(r"我(?:在|去)([\u4e00-\u9fa5]{2,12}?(?:大学|学校|公司|上班|实习))", t)
        if m:
            out.append(("所在", m.group(1)))
        return out

    @staticmethod
    def extract(user_text, api_config=None):
        """提取入口：API 优先，失败降级规则"""
        api = MemoryStore.extract_with_api(user_text, api_config)
        if api is not None:
            facts, events = api
            return facts, events, "api"
        facts = MemoryStore.extract_rule(user_text)
        return facts, [], "rule"

    @staticmethod
    def extract_her_promise(her_text, api_config=None):
        """从**她**说的话里抽承诺/约定。

        和 extract 的区别在提示词：extract 面对的是用户的话，抽出来的是"他的事"
        （喜好、习惯、计划）；这里面对的是她自己的话，要抽的是"她答应了什么"。
        视角反了，同一句提示词会抽出错的东西 —— 用户说"我去接你"是用户要接，
        她要记的却是"她答应去接"，所以提示词里必须写清主语。

        返回 ([事件], [], mode)，跟 extract 同结构好统一处理。
        """
        if not api_config or not api_config.get("api_key"):
            return [], [], "skip"
        prompt = (
            "你是记忆助手。下面这句是**她自己**说的话（不是对方说的），"
            "请从中提取她对对方许下的承诺、约定或自己定下的近期安排。\n"
            "规则：\n"
            "1. 统一用第三人称「她」当主语，不要出现「我」「助手」「AI」。"
            "说「我下午去接你」→ 记成「她下午去接他」。"
            "写成「地点由我（助手）决定」这种等于没抽。\n"
            "2. 只记已经说定的安排。假设句（「要是…」「…也行」「随便」）"
            "和玩笑一律不记，返回空列表。\n"
            "3. 状态播报（「你那边17度」「出门记得穿外套」）和回顾自己的心情"
            "（「先把高数对付过去再说」）不是安排，不记。\n"
            "4. 每条用一句话，含时间/地点/动作，能让人一眼看懂。\n"
            "输出严格 JSON：{\"events\": [\"一句话\"]}\n"
            "没有就输出 {\"events\": []}。\n"
            f"她说的话：{her_text}"
        )
        try:
            r = httpx.post(
                api_config["api_base"].rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_config["api_key"]},
                json={"model": api_config.get("model", "deepseek-chat"),
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 200},
                timeout=25,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?|```$", "", content,
                                 flags=re.M).strip()
            data = json.loads(content)
            events = []
            for e in data.get("events", []):
                if not e:
                    continue
                e = str(e)
                # 提示词里要求第三人称，但模型偶尔会写成"我（助手）决定"这种
                # 第三方转述 —— 那样注入上下文时她读到会以为有人在说自己。
                # 提示词不是保证，这里做一次兜底替换。
                if any(w in e for w in ("（助手）", "(助手)", "助手决定",
                                        "AI决定", "AI 决定", "由我决定")):
                    e = e.replace("（助手）", "").replace("(助手)", "")
                    e = e.replace("我决定", "她决定")
                for bad, good in (("由我（助手）", "由她"), ("我（助手）", "她"),
                                  ("由我决定", "由她决定"), ("我来", "她来")):
                    e = e.replace(bad, good)
                if e.strip():
                    events.append(e)
            return events, [], "api"
        except Exception:
            return [], [], "fail"
