# -*- coding: utf-8 -*-
"""插件加载器：把 brain/plugins/ 下的每个 .py 当成独立功能块挂进她的链路。

**为什么有这个**：以前加一个功能（比如闹钟提醒）要新建 alarm.py、改 reply.py、
改 server.py、改 persona_store 的提示词，四处都动 —— 改多了 reply.py 就成一堆
if-else，改一个功能还可能弄坏别的。插件化之后，核心只认下面这几个钩子，
新功能只需要往 plugins/ 里丢一个文件，核心代码一行不改。

## 一个插件文件长什么样

    NAME = "reminder"          # 必填，插件名（英文，代码里引用它）
    DESC = "闹钟/提醒"          # 必填，给人看的
    FEATURE = "reminder"       # 选填，声明后 App 设置页自动多一个开关

    def prompt(ctx): ...            # 选填 → 拼进 system：告诉她"你有这个本事"
    def on_turn_in(ctx): ...        # 选填 → 他发来消息时，返回要注入 system 的提示
    def on_reply(ctx, text): ...    # 选填 → 回复加工完（剥标记/生图/语音之后）
    def start(brain): ...           # 选填 → 自己的后台线程，进程启动时调一次
    TOOLS = {"名字": 函数}          # 选填 → 模型可以用 [tool:名字:参数] 调它
    TOOL_HELP = "..."               # 选填 → 教模型怎么用，随 tools 一起进提示
    ROUTES = [("POST", "/api/plugin/xxx", 处理函数)]   # 选填 → 自己的 HTTP 接口

钩子都是可选的，写哪个算哪个。返回值约定：
- prompt / on_turn_in 返回字符串（或 None），拼进给模型的 system
- on_reply 必须返回字符串；不想动就原样返回，**别返回 None**

## 三条纪律

1. **插件坏了不能拖累她**：每个钩子都单独 try/except，炸了只打印一行，
   on_reply 出错就返回原文。插件进程内隔离不了 import 副作用，所以插件里
   禁止在模块顶层做网络请求 —— 要用就在钩子里调。
2. **开关是黑名单式的**：FEATURE 声明了就受 features 段管（默认开，
   App 能关）；没声明就永远开。别自己在钩子里读 config 判开关，那等于
   把白名单又写死一遍。
3. **插件不认识就是不认识**：run_tool 查不到名字就返回 None，她照实说查不到。
   模型编出来的工具名由 tools.TOOL_TAG_ANY_RE 兜底扫掉，不会漏给用户。
"""
import importlib
import importlib.util
import os
import sys
import threading
import traceback

try:
    from paths import PLUGINS_DIR, PLUGIN_DATA_DIR, CONFIG_DIR
    _DIR = str(PLUGINS_DIR)
    _DDIR = str(PLUGIN_DATA_DIR)
    _ROOT = str(CONFIG_DIR)
except Exception:
    _DIR = r"C:\linzhixia\plugins"
    _DDIR = r"C:\linzhixia\data\plugins"
    _ROOT = r"C:\linzhixia\config"

# 钩子名 → 必需参数个数。用于参数个数不对时打一行日志而不是抛 TypeError
# 到调用方栈里（插件作者看到的应该是"你这里少传了参数"，不是她的调用栈）。
_ARITY = {"prompt": 1, "on_turn_in": 1, "on_reply": 2, "start": 1}

_registry = {}        # name -> Plugin
_lock = threading.RLock()
_loaded = False


def _log(tag, msg):
    print("[插件] %s：%s" % (tag, msg), flush=True)


# --- 一轮对话的上下文：插件能看见什么、又能改什么 ---

class Turn(object):
    """插件拿到的一轮上下文。

    只放"插件真的可能需要"的字段 —— 塞满整个 Brain 等于把封装拆了，
    插件就会开始直接改她的内部状态，出问题没法查。
    """

    def __init__(self, text="", src="app", api_config=None, brain=None):
        self.text = str(text or "")          # 他这一轮说的话（原值，含插件前处理）
        self.src = str(src or "app")         # 渠道：app / wechat
        self.api_config = api_config or {}   # 共享的那份 API_CFG
        self.brain = brain                   # 大脑对象，只读用（问问题、查记忆）
        self.meta = {}                       # 插件自己的中途数据，同轮内互相传

    def say(self, text):
        """让插件能主动开口（跟 proactive 那条路一样，App 轮询会弹通知）。"""
        try:
            from proactive import _cloud_append_assistant
            _cloud_append_assistant(str(text or ""))
            return True
        except Exception:
            return False

    def ask(self, prompt, proactive=True):
        """让插件借她的嘴说一句（记忆/生活/摘要都会正常走一遍）。"""
        try:
            if self.brain is None:
                return ""
            ans, _mode = self.brain.chat(str(prompt or ""),
                                         proactive=proactive, log=False)
            return str(ans or "").strip()
        except Exception:
            return ""

    def data_dir(self):
        """插件自己的持久化目录（data/plugins/<name>/），不存在就建好。"""
        d = os.path.join(_DDIR, self.owner or "_shared")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d

    # data_dir 要知道是谁在问，但 Turn 是所有插件共用的对象；
    # 派发时临时挂一个 _owner，比每个钩子多传一个参数干净
    _owner = ""


# --- 插件对象 ---

class Plugin(object):
    def __init__(self, mod, path):
        self.name = str(getattr(mod, "NAME", "") or "").strip()
        self.desc = str(getattr(mod, "DESC", "") or "").strip()
        self.feature = str(getattr(mod, "FEATURE", "") or "").strip()
        self.mod = mod
        self.path = path
        self.tools = dict(getattr(mod, "TOOLS", {}) or {})
        self.tool_help = str(getattr(mod, "TOOL_HELP", "") or "").strip()
        self.routes = list(getattr(mod, "ROUTES", []) or [])
        self.err = ""

    def hook(self, name):
        """取钩子函数，没有就返回 None。"""
        return getattr(self.mod, name, None)

    def info(self):
        """给 /api/plugins 用的自述。"""
        return {
            "name": self.name,
            "desc": self.desc,
            "feature": self.feature,
            "tools": sorted(self.tools.keys()),
            "routes": [r[1] for r in self.routes if len(r) >= 2],
            "hooks": sorted(n for n in _ARITY if callable(self.hook(n))),
            "file": os.path.basename(self.path),
            "err": self.err,
        }


# --- 加载 ---

def _load_one(path):
    """把一个 .py 当插件模块导进来。返回 Plugin 或 None（失败只打一行）。"""
    name0 = os.path.splitext(os.path.basename(path))[0]
    try:
        spec = importlib.util.spec_from_file_location("zxplug_" + name0, path)
        if spec is None or spec.loader is None:
            raise ImportError("拿不到模块规格")
        mod = importlib.util.module_from_spec(spec)
        # 放进 sys.modules 再 exec：插件内部 import 自己或被别的插件 import 时才找得到
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        p = Plugin(mod, path)
        if not p.name:
            raise ValueError("模块里没写 NAME")
        if not re_name_ok(p.name):
            raise ValueError("NAME 只能用英文/数字/下划线/短横线：%r" % p.name)
        return p
    except Exception as e:
        _log(os.path.basename(path), "加载失败，已跳过：%s" % str(e)[:160])
        return None


def re_name_ok(n):
    """插件名的合法字符：只准字母数字下划线短横线。

    名字会进 features 段（config.json 的键）和提示词，白名单收到最紧 ——
    它不是给模型用的标识符，是给人看的开关名。
    """
    return bool(n) and all(c.isalnum() or c in "_-" for c in n)


def load(force=False):
    """扫 plugins/ 目录装载全部插件。进程启动时调一次，改完插件可再调（热重载）。

    只认 .py、跳过 __init__.py 与下划线开头的文件（那些是模块不是插件）。
    目录不存在就当没有插件 —— plugins/ 是空的也完全不影响聊天。
    """
    global _loaded
    with _lock:
        found = {}
        try:
            names = sorted(os.listdir(_DIR))
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            p = _load_one(os.path.join(_DIR, fn))
            if p:
                found[p.name] = p
        # 换掉的模块要从 sys.modules 摘掉，否则热重载拿到的是旧代码
        for k in [k for k in sys.modules if k.startswith("zxplug_")]:
            if k not in [ "zxplug_" + os.path.splitext(
                    os.path.basename(v.path))[0] for v in found.values()]:
                sys.modules.pop(k, None)
        _registry.clear()
        _registry.update(found)
        _reindex_tools()
        _loaded = True
        _sync_features()
        _log("装载", "共 %d 个：%s" % (
            len(found), "、".join(sorted(found)) or "无"))
        return dict(found)


def _sync_features():
    """插件声明了 FEATURE 就登记成功能开关，App 设置页自动多一项。

    不声明的插件不受开关管（永远开）—— 想让它能被关掉，声明 FEATURE 就行，
    别自己在钩子里读 config 判开关。
    """
    try:
        import features
    except Exception:
        return
    for p in _registry.values():
        if p.feature:
            # 走 features.register 而不是直接改 DEFAULTS：那里还管着缓存失效
            features.register(p.feature, True)


def all_plugins():
    """当前在册的插件（name -> Plugin）。没装载过就返回空，不自动扫盘。"""
    with _lock:
        return dict(_registry)


def get(name):
    with _lock:
        return _registry.get(str(name or "").strip())


def listing():
    """给 /api/plugins：所有插件 + 开关状态。"""
    out = []
    try:
        import features
        on_map = features.all_features()
    except Exception:
        on_map = {}
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        d = p.info()
        d["enabled"] = (not p.feature) or bool(on_map.get(p.feature, True))
        out.append(d)
    return out


# --- 派发 ---

def _call(p, hook_name, *args):
    """调一个插件钩子，带护栏。

    返回 (是否成功, 值)。失败时值一律是 None，调用方决定怎么降级。
    错误只留一行 —— 完整栈打给 stdout 是噪音，但插件名和异常类型必须留。
    """
    fn = p.hook(hook_name)
    if not callable(fn):
        return True, None
    try:
        need = _ARITY[hook_name]
        if need == 2:
            return True, fn(*args)
        return True, fn(args[0] if need == 1 else None)
    except TypeError as e:
        # 参数个数不对单独说，这类错在调用方栈里看起来像核心代码炸了
        if "positional argument" in str(e) or "argument" in str(e):
            return False, _log(p.name, "%s 参数个数不对（要 %d 个）：%s"
                               % (hook_name, need, str(e)[:100]))
        return False, _log(p.name, "%s 调用失败：%s" % (hook_name, str(e)[:100]))
    except Exception as e:
        return False, _log(p.name, "%s 出错，已跳过：%s: %s"
                           % (hook_name, type(e).__name__, str(e)[:100]))


def prompt_block(ctx=None):
    """所有插件的 prompt 拼成一段，塞进 system。

    只拼"她有什么本事、怎么用"，不教"什么场景必须用" ——
    场景映射是那种永远补不完的规则（见 docs/插件系统.md 第二节）。
    """
    parts = []
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        if not _enabled(p):
            continue
        ok, v = _call(p, "prompt", ctx)
        if ok and isinstance(v, str) and v.strip():
            parts.append(v.strip())
    if not parts:
        return ""
    return "\n" + "\n\n".join(parts)


def on_turn_in(ctx):
    """他发来消息时，各插件可返回一段要注入 system 的提示。拼起来返回。

    对应以前硬写在 /api/chat 里的"检测到快递单号"那段 —— 现在那类东西
    归插件管，核心不认识具体功能。
    """
    out = []
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        if not _enabled(p):
            continue
        ok, v = _call(p, "on_turn_in", ctx)
        if ok and isinstance(v, str) and v.strip():
            out.append(v.strip())
    return "\n".join(out)


def on_reply(ctx, text):
    """回复加工链的最后一环：所有插件按名字顺序过一遍 text。

    出错的插件直接跳过，剩下的照走 —— 加工链的产物是要发给用户的，
    一个插件炸了不能让她发不出话。
    """
    cur = str(text or "")
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        if not _enabled(p):
            continue
        ctx._owner = p.name
        ok, v = _call(p, "on_reply", ctx, cur)
        ctx._owner = ""
        if ok and isinstance(v, str) and v:
            cur = v
    return cur


def run_tool(name, arg):
    """插件的工具。名字不在册返回 None（tools.run_tool 负责先问内置那三个）。"""
    p = get_by_tool(name)
    if not p or not _enabled(p):
        return None
    fn = p.tools.get(str(name or "").lower())
    if not fn:
        return None
    ctx = Turn(text=arg or "")
    ctx._owner = p.name
    try:
        return fn(arg) or None
    except Exception as e:
        return _log(p.name, "工具 %s 失败：%s" % (name, str(e)[:100]))


_TOOL_INDEX = {}


def _reindex_tools():
    """工具名 -> 持有它的插件。装载/热重载后重建一次。"""
    idx = {}
    for p in _registry.values():
        for t in p.tools:
            idx[str(t).lower()] = p
    _TOOL_INDEX.clear()
    _TOOL_INDEX.update(idx)


def get_by_tool(name):
    return _TOOL_INDEX.get(str(name or "").lower())


def tool_help():
    """插件工具的用法说明，拼进"工具"那一段提示。

    每条以换行开头而不是直接接在分号后面 —— 拼进去的位置前面是
    "[tool:tracking:单号] 查物流；"，不加换行会读成"；- 算式：…���"，
    模型对这种黏在一起的列表分辨力明显变差。
    """
    out = []
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        if not _enabled(p) or not (p.tool_help or p.tools):
            continue
        line = p.tool_help or ("、".join("[tool:%s]" % t
                                     for t in sorted(p.tools)))
        if line:
            out.append("\n" + line.strip())
    return "".join(out)


def route_table():
    """所有插件的 HTTP 路由，格式 (method, path, handler, plugin_name)。"""
    out = []
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        for r in p.routes:
            if len(r) < 3:
                _log(p.name, "ROUTES 项不完整（要 method, path, handler）：%r" % (r,))
                continue
            out.append((str(r[0]).upper(), str(r[1]), r[2], p.name))
    return out


def start_all(brain):
    """把带 start 的插件的后台线程跑起来（run_server 装完大脑后调一次）。"""
    n = 0
    for p in sorted(all_plugins().values(), key=lambda x: x.name):
        if not _enabled(p) or not callable(p.hook("start")):
            continue
        ok, _v = _call(p, "start", brain)
        if ok:
            n += 1
    if n:
        _log("启动", "%d 个插件的后台循环已起" % n)


def _enabled(p):
    """这个插件此刻开没开。没声明 FEATURE = 永远开。"""
    if not p.feature:
        return True
    try:
        import features
        return bool(features.on(p.feature))
    except Exception:
        return True
