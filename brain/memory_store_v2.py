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
import json, os, re, time, sys
import numpy as np
import httpx

# 云端嵌入模型（1024 维）。改这里会自动触发索引重建，不用手工删向量文件
EMBED_API_MODEL = os.environ.get("EMBED_API_MODEL", "BAAI/bge-m3")
# 本地兜底模型（要能连 HuggingFace 才用得上）
LOCAL_EMBED_MODEL = "BAAI/bge-small-zh-v1.5"

_embedder = None
_embed_dead = False     # 本进程内已确认 embedding 不可用，别再每轮白等


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


class MemoryStore:
    def __init__(self, path, embed_dir=None):
        self.path = path
        self.embed_dir = embed_dir or os.path.dirname(os.path.abspath(path))
        self.data = {"items": [], "meta": {"created": time.strftime("%Y-%m-%d %H:%M")}}
        self._load()
        # ---- 向量索引（懒加载，加载一次后进内存）----
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

    # ---------- 持久化 ----------
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

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # --- 向量索引 ---
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
    def add(self, text, mtype="event"):
        """添加一条记忆（自动向量化）"""
        text = text.strip()
        if not text:
            return
        for it in self.data["items"]:
            if it["text"] == text:
                return
        item = {"id": f"m{int(time.time()*1000)}{len(self.data['items'])}",
                "type": mtype, "text": text,
                "time": time.strftime("%Y-%m-%d %H:%M")}
        self.data["items"].append(item)
        self.save()
        # 向量化并追加到内存索引
        self._load_index()
        if item["id"] in self._imap:
            return
        vs = embed_texts([text])
        if not vs:
            return                                  # 向量化不了也先把记忆存下来
        v = vs[0]
        if self._rows and v.shape[0] != self._rows[0].shape[0]:
            self._reset_index()                     # 维度变了，交给 _backfill 全量重算
            self._backfill()
            return
        self._imap[item["id"]] = len(self._rows)
        self._ids.append(item["id"])
        self._rows.append(v)
        self._mat = None
        self._dim = v.shape[0]
        self._save_index()

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

    def list_items(self):
        return list(self.data["items"])

    # --- 向量检索 ---
    def retrieve(self, query, top_k=5, min_score=0.25):
        """按语义相似度返回 Top-K 记忆文本。索引在内存里，不逐条读盘"""
        if not self.data["items"]:
            return []
        self._load_index()
        M = self._matrix()
        if M.shape[0] == 0:
            return []
        vs = embed_texts([query])
        # 拿不到向量、或维度跟索引对不上（换过模型还没重建）→ 降级：最近几条
        if not vs or vs[0].shape[0] != M.shape[1]:
            return [i["text"] for i in self.data["items"][-top_k:]]
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

    def render(self, query=None, top_k=5, max_items=20, min_score=0.40):
        """注入块：有 query 用检索，无 query 用最近 max_items 条。

        min_score 取 0.40 而不是 0.25：0.25 太松，随便一句闲聊都能拉出 5 条"沾边"的旧记忆
        塞进上下文，她的话题和情绪被旧事带跑。宁可少喂，不可喂错 —— 记忆是调味，不是主菜。
        """
        if query:
            hits = self.retrieve(query, top_k=top_k, min_score=min_score)
            self.last_hits = hits          # 给 trace 落盘用（2026-10-01 加）
            if hits:
                return "你记得他的事（这些是真的，聊天时自然地用上）：\n" + "；".join(t for _, t in hits)
        items = self.data["items"][-max_items:]
        if not items:
            return ""
        return "你记得他的事（这些是真的，聊天时自然地用上）：\n" + "；".join(i["text"] for i in items)

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
            out.append((f"宠物", f"{m.group(1)}叫{m.group(2)}"))
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
