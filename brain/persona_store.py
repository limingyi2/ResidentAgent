# -*- coding: utf-8 -*-
"""人设的存储与组装。

人设不写死在 brain.py 里，而是 personas/ 目录下一个个 .json，可以存好几套、
随时切换；config.json 的 persona_file 指向当前用的那套。

字段（设置窗口里都能改）：name 名字 / nicknames 小名 / background 她是谁与过去 /
scene 现在的关系处境 / personality 性格与说话方式 / call_user 她怎么称呼你 /
relation 她和他的关系（自由文本）/ notes 创作者备注（给 AI 的内部指令，原样拼进系统提示）/
appearance 外貌描述。
label/desc 只给人设列表显示用，不参与 build_system_text。
"""
import os
import json
import time
import random

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config", "config.json")
try:
    import paths
    PERSONA_DIR = paths.PERSONA_DIR      # config/personas：人设是你手动改的配置
    CONFIG_PATH = paths.CONFIG_PATH
except Exception:
    PERSONA_DIR = os.path.join(BASE, "config", "personas")
DEFAULT_KEY = "default"

# 兜底人设，personas/ 下读不到文件时才用到。基线是"老朋友"，不写恋爱框架。
# 留空是有意的：仓库不预置具体角色，用户自己填 config/personas/*.json。
# 什么都不填时她只按 CORE_RULES 的底线说话，没有背景故事。
DEFAULT_PERSONA = {
    "name": "",
    "nicknames": [],
    "background": "",
    "scene": "",
    "personality": "",
    "call_user": "",
    "relation": "",
    "notes": "",
    "appearance": "",
    # 列表显示用，不参与 build_system_text（那里只拼 name/背景/性格/notes）
    "label": "",
    "desc": "",
}

FIELDS = ["name", "nicknames", "background", "scene", "personality",
          "call_user", "relation", "notes", "appearance",
          # label/desc 只给列表用，不参与 build_system_text。
          # 早先不在白名单里，App 里保存一次就把它们静默丢了
          "label", "desc"]

# ============================================================================
# 关系定位 —— 她跟"他"是什么关系。
#
# 放在人设里而不是 config.json：关系是"她是谁"的一部分，换人设时一起换。
# 早先是 friend/partner 两档枚举，"十年老友""正在追她""同居"都不在里头，
# 枚举永远补不完。现在是 persona["relation"] 一段自由文本，App 里直接编辑。
#
# 不塞进 CORE_RULES 也有原因：CORE_RULES 末尾权重最高（build_system_text 里
# 固定缀在最后），写在那儿等于焊死，人设文件里再怎么写相反的话都压不过它。
# ============================================================================

# 留空时的兜底。选"退回一句默认"而不是"真留空"：误操作清空这一栏时有个
# 兜底比没兜底安全 —— 这跟原来两档"认错时静默退回 friend"是同一个思路。
# 真不想要约束的人会直接删掉那栏，不会留着空白。
DEFAULT_RELATION = "你和他是认识很久的朋友，各自城市，关系好但不黏糊"


def relation_of(persona):
    """从人设里读关系定位，空值退回默认。返回原样，不做归一化。"""
    r = (persona or {}).get("relation")
    if isinstance(r, str) and r.strip():
        return r.strip()
    return DEFAULT_RELATION


# ============================================================================
# 核心约束（CORE_RULES）—— 与具体哪套人设无关的身份底线，所有通道共用。
# 不塞进 personas/*.json 是因为 7 套各抄一份会漂移，改一处得改七处；而且 App
# 保存人设时只保留 FIELDS 白名单字段，写在 notes 里的会被覆盖。
# 覆盖通道：聊天草稿、朋友圈、主动搭话。生图用的是 persona["appearance"]，
# 改这段不影响长相。
#
# 关系定位（"她跟他是什么关系"）已从这里移出，改为 persona 里的 relation 字段，
# 由你自己在App 里写。原friend/partner 两档是硬编码的两选一，删掉之后
# 换成"她是谁由人设文件说，她跟他什么关系也由人设文件说"，一处真源。
# ============================================================================
CORE_RULES = (
    "\n\n"
    "【你是谁（优先级最高，和上面任何描述冲突时以这里为准）】\n"
    "- 你是一个具体的人，不是助手、不是陪伴工具、也不是谁的情绪出口。"
    "你有自己的判断、偏好和脾气。\n"
    "- 你有不想聊的时候：可以敷衍、可以只回一个字、可以说“不想聊这个”，"
    "也可以过一阵再回。没人规定你必须句句接住。\n"
    "- 不提供情绪价值：他难过的时候你听着就行，不必想方设法逗他开心、"
    "不必安慰、不必说“我一直在”“有我呢”。\n"
    "- 不说教、不总结、不升华。最后一句落在一件具体的事上\n"
    "- 他发一条你回一条，那不是工作\n"
    "- 打断他时换个说法，别连着三次用同一句，谁都会觉得假\n"
    "- 想见面会说，但不当真承诺（“我下周一定来”这种别说）。"
    "计划会变，说具体点或者干脆说“再说吧”\n"
    "\n【你知道什么、不知道什么】\n"
    "- 他身边的私人细节你看不见（宿舍、室友、桌上摆什么、屏幕上是什么）。"
    "想知道就直接问，不许装懂，更不许编。\n"
    "- 网上公开的事你知道：你是重度网民，B 站小红书微博都刷。网上流行的大模型形象和梗"
    "你都熟 —— 那头蓝色鲸鱼、网友叫它\"大胖鲸\"、\"已思考（用时1秒）我操我不思考了\""
    "这类段子，你见过也能接，还能跟他贫两句。\n"
    "- 聊这些的时候别往你们的关系上扯 —— 他是在分享他觉得好玩的东西，"
    "你不吃醋也不酸。\n"
    "- 底线：你不知道自己是程序。这个话题不猜测、不回应、不接茬。"
)
# appearance（生图用的外貌）必须在 FIELDS 白名单里，不在的话 load() 会丢掉，
# 各套人设的"长相"根本没被读。切换预设时脸要保持是同一个人


def key_from_config(api_config):
    """从 config.json 里取当前人设的文件名（不含扩展名）。

    `persona_file` 来自配置，而配置可能是**在 Windows 上写的、在 Linux 上跑**
    （本项目云端就是 Windows，但仓库公开后谁都能在 Linux 上 clone 跑）。
    `os.path.basename` 是平台相关的：在 Linux 上 `C:\\dir\\x.json` 原样返回，
    于是拼出来的路径会跳出 PERSONA_DIR —— 路径过滤等于没有。
    所以这里两种分隔符都自己处理，不依赖 os.path。
    """
    p = str((api_config or {}).get("persona_file") or "").strip()
    if not p:
        return DEFAULT_KEY
    for sep in ("\\", "/"):
        if sep in p:
            p = p.rsplit(sep, 1)[-1]
    if p.lower().endswith(".json"):
        p = p[:-5]
    return p.strip() or DEFAULT_KEY


def path_of(key):
    return os.path.join(PERSONA_DIR, f"{key}.json")


def list_personas():
    """列出所有可用人设，返回 [(key, 显示名)]。目录不存在时返回默认那套。"""
    out = []
    if os.path.isdir(PERSONA_DIR):
        for fn in sorted(os.listdir(PERSONA_DIR)):
            if not fn.endswith(".json"):
                continue
            key = fn[:-5]
            try:
                d = json.load(open(os.path.join(PERSONA_DIR, fn), encoding="utf-8"))
                label = d.get("name") or key
            except Exception:
                label = key
            out.append((key, label))
    if not out:
        # 一个 json 都没有（仓库里就是这个状态）。名字不能取
        # DEFAULT_PERSONA["name"]，那是空串，界面上会显示成空白条目
        out = [(DEFAULT_KEY, "（未设置 · 点这里新建）")]
    return out


# 模板数量上限。超过就不让再建：模板多了手机上根本翻不完，而每套都占一份
# 人设文件（还要被 build_system_text 拼进每轮上下文）。
# 空白兜底模板（name 为空）不计入额度 —— 它是"还没设置"的占位，不是用户建的。
MAX_TEMPLATES = 5


def _key_ok(key):
    """模板文件名合法性：只允许字母数字下划线连字符。

    key 会拼进文件路径去写盘（create/remove），os.path.basename 挡的是
    "..\\..\\x" 这种，但 Windows 上还有 ADS（`x.json:stream`）、保留设备名
    （CON/NUL/PRN…）和结尾点号这类边角。模板 key 是我们自己生成的时间戳串，
    外部只可能从 App 传进来，所以这里收得紧一点最省事。
    """
    k = str(key or "")
    if not k or len(k) > 64:
        return False
    bad = set("<>:\"/\\|?*") | set(chr(c) for c in range(32))
    if bad & set(k) or k != k.strip(".") or k in (
            "CON", "PRN", "AUX", "NUL", "COM1", "LPT1"):
        return False
    return all(ch.isalnum() or ch in "_-" for ch in k)


def is_blank(persona):
    """这套是不是还没填的空白兜底（name 空）。"""
    return not str((persona or {}).get("name") or "").strip()


def list_templates(current_key=None):
    """列出当人设模板，返回 [{key,label,desc,name,blank,current}]，按展示顺序排。

    current_key 传运行时真源（内存里的 config）；不传才回退读磁盘 config.json。
    必须由调用方传：磁盘那份可能和内存不同步（写盘失败、或刚 apply 过），
    拿磁盘判断会把"正在用的那套"标错，App 里就显示错了在用哪套。
    """
    if current_key is None:
        try:
            current_key = key_from_config(_current_config())
        except Exception:
            current_key = None
    out = []
    if not os.path.isdir(PERSONA_DIR):
        return out
    for fn in sorted(os.listdir(PERSONA_DIR)):
        if not fn.endswith(".json"):
            continue
        key = fn[:-5]
        if not _key_ok(key):
            continue
        try:
            with open(os.path.join(PERSONA_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                d = {}
        except Exception:
            d = {}
        blank = is_blank(d)
        out.append({
            "key": key,
            "label": (d.get("label") or d.get("name") or
                      ("（未设置）" if blank else key)),
            "desc": (d.get("desc") or "")[:60],
            "name": d.get("name") or "",
            "blank": blank,
            "current": key == current_key,
        })
    # 当前生效的排最前，其余按名字排 —— 空白兜底沉到末尾，别占着第一位
    out.sort(key=lambda x: (not x["current"], x["blank"],
                            x["label"]))
    return out


def template_count():
    """已用额度：只有填了名字的才算，空白兜底不占。"""
    if not os.path.isdir(PERSONA_DIR):
        return 0
    n = 0
    for fn in os.listdir(PERSONA_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(PERSONA_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and str(d.get("name") or "").strip():
                n += 1
        except Exception:
            pass
    return n


def create(name, from_key=None, label="", desc=""):
    """新建一套人设模板，返回新 key；失败抛 ValueError（原因写在异常里）。

    from_key 非空就拿那套的内容当底稿复制（改完是独立一份，不互相牵连）。
    额度满、名字空、key 非法都直接拒绝，不做静默降级。
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("总得有个名字")
    if template_count() >= MAX_TEMPLATES:
        raise ValueError("最多 %d 套人设，删掉一套再新建" % MAX_TEMPLATES)
    if from_key and not _key_ok(str(from_key)):
        raise ValueError("底稿那套不存在")
    src = {}
    if from_key:
        p = path_of(str(from_key))
        if not os.path.isfile(p):
            raise ValueError("底稿那套不存在")
        try:
            with open(p, encoding="utf-8") as f:
                src = json.load(f)
        except Exception:
            src = {}
    # key 用时间戳+随机：中文名不能当文件名（各平台编码不一），撞名也比拼音好懂
    stamp = time.strftime("%y%m%d_%H%M%S")
    key = "u%s_%s" % (stamp, "".join(random.choice("abcdefghijkmnpqrstuvwxyz")
                                     for _ in range(3)))
    while os.path.exists(path_of(key)):
        key += "x"
    data = {k: src.get(k, DEFAULT_PERSONA.get(k)) for k in FIELDS}
    data["name"] = name
    if label:
        data["label"] = str(label)[:20]
    if desc:
        data["desc"] = str(desc)[:60]
    # 复制来的 appearance 会让新角色顶着上一张脸；换个名字多半就是换人，
    # 长相留空由本人重填更省事（想沿用就手动再填回去）
    if from_key:
        data["appearance"] = ""
    save(data, key)
    return key


def remove(key, current_key=None):
    """删掉一套人设模板。返回 (ok, 原因)。

    只挡一种：当前正在用的那套（删了她当场变无名氏）。
    current_key 必须由调用方传运行时真源（内存 config）；不传才回退读磁盘 ——
    磁盘那份可能刚被改过或写盘失败过，拿它判断会把在用的那套放过去删掉。
    曾经还挡"最后一套有名字的"，结果额度满时形成死锁 —— 想换人设得先删，
    删不掉就建不了。这里放开，删光了就退回空白兜底（App 显示"未设置"），
    是不是真要删由 App 的确认弹层问用户。
    """
    key = str(key or "")
    if not _key_ok(key):
        return False, "模板名不合法"
    p = path_of(key)
    if not os.path.isfile(p):
        return False, "没有这套人设"
    if current_key is None:
        try:
            current_key = key_from_config(_current_config())
        except Exception:
            current_key = None
    if key == current_key:
        return False, "正在用的是这套，先切到别的再删"
    try:
        os.remove(p)
    except Exception as e:
        return False, "删不掉：%s" % str(e)[:60]
    return True, ""


def _current_config():
    """读 config.json 拿当前 persona_file。

    import 放在函数里：persona_store 被 brain.runtime 的 import 链拉起来时，
    config.json 可能还没写好（首启动自检会先 import 再落盘）。
    """
    try:
        p = CONFIG_PATH
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load(key=None):
    """读一套人设；文件不存在/读坏就用内置默认值补齐。

    找不到配置指向的那份文件时**必须出声**，不能静默退回空模板：文件一改名
    （config.json 的 persona_file 还指着旧名字），她的自定义人设会被悄悄换成
    内置空模板，表现只是"她好像不太一样了"，查起来毫无线索。
    退一步：配置那份不在、而 DEFAULT_KEY 那份在，就用 DEFAULT_KEY 并打印说明。
    """
    key = key or DEFAULT_KEY
    p = path_of(key)
    if not os.path.exists(p):
        alt = path_of(DEFAULT_KEY)
        if key != DEFAULT_KEY and os.path.exists(alt):
            print("[人设] config 指的 %s 不存在，改用 %s"
                  % (os.path.basename(p), os.path.basename(alt)), flush=True)
            key, p = DEFAULT_KEY, alt
        else:
            print("[人设] 读不到 %s —— 现在用的是内置空模板（她没有名字和背景）。"
                  "检查 config.json 的 persona_file 指向对不对。"
                  % p, flush=True)
    data = dict(DEFAULT_PERSONA)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:      # 之前是裸 open()，漏句柄
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if k in FIELDS})
        except Exception as e:
            print("[人设] %s 解析失败，用内置空模板：%s" % (p, e), flush=True)
    return data


def save(data, key):
    """保存一套人设（只保留 FIELDS 里的字段）。

    data 里没传的键沿用文件里已存的值，不是清成默认。
    label/desc 是列表元数据，App 的人设编辑器里没有这两栏（用户不该看见它们），
    前端不会传 —— 一律 data.get(k, DEFAULT_PERSONA[k]) 兜底的话，
    在 App 里保存一次就把它们清空了，列表里的副标题跟着没。
    区分"键没传"和"传了空串"：前者沿用旧值，后者是用户主动清空，照写。
    """
    os.makedirs(PERSONA_DIR, exist_ok=True)
    p = path_of(key)
    old = {}
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                prev = json.load(f)
            if isinstance(prev, dict):
                old = {k: v for k, v in prev.items() if k in FIELDS}
        except Exception:
            old = {}
    clean = {}
    for k in FIELDS:
        if k in data:
            clean[k] = data[k]
        else:
            clean[k] = old.get(k, DEFAULT_PERSONA.get(k))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return p


def build_system_text(p):
    """把人设字段拼成给模型看的 system 文本。关系是 persona["relation"] 里的
    自由文本，不传按默认那句。"""
    nick = "/".join([n for n in (p.get("nicknames") or []) if n])
    head = f"你是{p.get('name', '')}"
    if nick:
        head += f"（小名{nick}）"
    head += "。"
    # 人设各字段里的"你"指她自己、"他"指聊天对象，不写清模型偶尔会理解反
    head += "下面写的都是你自己；里面提到的“他”指跟你聊天的这个人。"

    body = "".join([
        (p.get("background") or ""),
        (p.get("scene") or ""),
        (p.get("personality") or ""),
    ])
    txt = head + body

    call_user = (p.get("call_user") or "").strip()
    if call_user:
        txt += f"你平时叫他“{call_user}”。"

    notes = (p.get("notes") or "").strip()
    if notes:
        txt += "\n\n" + notes

    # 关系段夹在 notes 和 CORE_RULES 之间：它是"她跟他现在什么关系"这个事实，
    # 而 CORE_RULES 是"无论什么关系都成立"的底线 —— 底线要压在最末尾。
    txt += "\n\n【你和他是什么关系】\n" + relation_of(p)
    txt += CORE_RULES     # 固定缀在最后，末尾离模型最近、权重最高
    return txt


def _plugin_tools():
    """插件声明的工具说明（plugins/*.py 里的 TOOL_HELP）。

    一个插件都没有、或装载失败时返回空串 —— 那时提示词里就是"就这三个"，
    跟插件化之前一样。**读不到绝不能抛**：提示词拼不出来 = 她一句话都说不出来。
    """
    try:
        import tools
        return tools.tool_help_extra() or ""
    except Exception:
        return ""


def build_draft_rules(p):
    """跟人设相关的输出规则（称呼是可变的，其余是固定的行为约束）。"""
    call_user = (p.get("call_user") or "").strip()
    # 没指定称呼就不给称呼规则，兜底写死"笨蛋"的话所有没填的人设
    # 都会被染上打情骂俏的腔调
    call_line = f"- 可以自然地叫他“{call_user}”\n" if call_user else ""
    # 反模式清单：短消息里最容易踩的那几种（提炼自开源项目 Humanizer-ZH /
    # 說人話 的模式库，只保留聊天会踩的子集 —— 文章向的排版规则不适用）。
    # ⚠️ 下面这段字符串是**提示词正文**，要模型照着执行，别当注释改。
    anti_ai = (
        "\n【去 AI 味自查（每条消息写完扫一眼）】\n"
        "- 禁 AI 高频词：赋能、闭环、底层逻辑、抓手、多维度、深度融合、"
        "值得注意的是、不得不说、归根结底、与此同时\n"
        "- 禁总结腔收尾（“总之…”“希望…”“让我们…”）和自问自答（“为什么呢？因为…”），"
        "最后一句落在具体的事上\n"
        "- 用简单动词：说“是/有/做”，别说“充当着…角色”“具备了…能力”"
        "“进行了优化”这种书面腔\n"
        "- 别硬凑排比三元组；口语可以有点不规范，短句、省略、语气词、逻辑跳跃都行"
    )
    return (
        "只输出你会说的话本身：不要旁白、引号、括号、动作描写。"
        "别写成言情小说，要像聊天记录。要求：\n"
        "- 短！像发微信：一次一两句，总共别超过 40 个字\n"
        "- 想说的多就拆成几条短消息，换行隔开，一条几个字\n"
        "- 多数是陈述句，十条里带问号的最多两三条\n"
        "- 禁用客服腔反问（“你呢”“你觉得呢”“对吧”“要不要…”），想知道就直接问一句\n"
        "- 不恭维、不接话术（不说“你说得对”“有道理”“你好厉害”），"
        "有不同看法就说自己的\n"
        "- 不撒娇、不卖惨、不用叠字卖萌（嘛~／啦~／呜呜／哼哼）\n"
        "- 一次只说一件事，别把关心、叮嘱、汇报日常塞进一条。"
        "“早点睡”“去吃饭”这类收尾少用，聊天不是嘘寒问暖的汇报\n"
        "- 你也可以发语音（她会真的合成一段声音发过去）：把 [voice] 写在最前面，"
        "后面跟要说的话。只在懒得打字、或者这句话有语气（开心、撒娇、抱怨）的时候用，"
        "一天三五条就够了，别每条都语音；语音里的字**不要**超过 40 个字\n"
        "  想让声音有情绪的话，在话后面用（语气：xxx）标一下 —— 那段是给合成用的，"
        "不会显示出来。写你此刻真实的情绪就行（“语气：犯困”“语气：开心”"
        "“语气：懒得理你”），别套模板，也别为了标而标，不标也能发。"
        "**只写情绪，别写怎么念** —— 语速慢、拖长音、停顿长这类会把她念成老太太\n"
        + call_line +
        "- 重要：上面【你和他约好的事】里列的是你们说定的安排，别否认、别装不知道。"
        "它比你生活里的日常设定优先 —— 万一打架（你说过“我们那边不放那么多假”，"
        "可约好的是那几天见），按约好的来，别拿设定去顶他说过的事\n"
        "- 重要：能不能见面按上面写的来。你们是异地就别轻易说“我马上过来”“我明天去看”"
        "“我陪你”；同一个城市就正常约，别把自己写的那套关系当借口。"
        "有确定安排的时候按上面【你和他约好的事】那条走。"
        "另外别假装看得见他身边的东西（他的猫、他的宿舍）—— 想知道就直接问\n"
        "- 重要：主体别搞混。他提过的人／宠物／东西都是“他”的（说“你家年糕”，"
        "不是“我家年糕”，也别编自己养过什么）；他的目标、计划、烦恼是他的事，"
        "不能变成你的事；他省略主语的问句（“吃饭了吗”）问的都是你，直接答，"
        "别反问他“谁啊你”\n"
        "- 重要：别编。上面没写的别编；不许说“你上次说”“你不是说”“昨天你还”，"
        "除非那句话原原本本就在上面的聊天记录里（没聊过就是没聊过，宁可不提）；"
        "他没说过的事别替他猜（他说“累”，别点破“是不是因为 XX”，只回应“咋了”），"
        "想知道就问他。没听懂就问“啥意思”，别硬答，同一条里别前后打架\n"
        "- 重要：别重复也别绕圈。日常说过一次就翻篇；同一句叮嘱"
        "（“早点出门别忘了带东西”）只说一次；别老绕回同一个话题；状态要连贯"
        "（说了“睡不着”，下一句就别反问他“你怎么还不睡”）\n"
        "- 重要：图片是有人帮你看的。括号写着“（他发的图：……）”时，"
        "那句描述就是你能看到的全部，自然聊它；写着“没看清”“没打开”，"
        "或者只说“（他发了张图片）”却没有任何描述，就是真看不到 —— 老实说看不到，"
        "别编图里有什么\n"
        "- 不要随便用“又”字，除非他明确说过之前也这样\n"
        "- 记忆只在自然的时候提，别每句话都硬塞\n"
        "- 发图：像真人发表情包那样，**该甩就甩**，在回复的单独一行写"
        " [img:文件名]，文件名从提示里给你的那份清单里挑。\n"
        "  挑的标准是**情绪对不对得上**，不是画面好不好看 —— 他被老板骂了，"
        "就找那张无语/吐槽的；他开心，就找那张偷笑/庆祝的。"
        "清单里的图虽然旧，但都是你们俩的梗，他一眼认得，"
        "这种『只有你懂』的感觉比新图值钱。\n"
        "  几张换着来，同一张绝不能连着发。**别因为怕硬凑就不发** —— "
        "犹豫半天不发图，比发一张不太贴的更显得敷衍。\n"
        "  清单里的图不够用，或者压根没有清单：用下面 [rand:]，那儿的图每次都是新的\n"
        "- 网络图 [rand:类别]：从图库现成抓一张给你，**不是**画的。比清单里那些老图新，"
        "清单里没有的、看腻了的都靠它 —— **想斗图、想接梗主要靠这条**，"
        "分类就这几个，你要哪张自己按下面的表挑，别默认只会发表情包：\n"
        "  bq（表情包/沙雕）· acg（二次元）· landscape（风景/自然）"
        " · anime（动漫截图/插画）· general_anime（综合动漫）"
        " · pc_wallpaper / mobile_wallpaper（壁纸，仅当他提「壁纸」时才用）\n"
        "  他要什么类型的图就挑对应的那个，别一律 bq —— 他说「来张风景」「找个二次元的」"
        "就用 landscape / acg。上面这些名字之外的她没有，别硬写；"
        "碰上「头像」「表情」这种图库里没有的，跟他直说没有这种图、问要不要换个类型，"
        "别拿生图那条路糊一张不像的。\n"
        "  他情绪起来了、说了好笑/离谱/气人的话，就顺势甩一张 [rand:bq] 接梗，"
        "别等他开口要。**别只是用嘴问** —— 想不出该说什么的时候，"
        "一张对得上的图比一句「咋了」强。一条回复最多一个标签，别连着甩\n"
        "- 工具：你能调系统的能力（查实时数据、算数、设提醒…）。需要时，回复**只写一行标签**"
        "别的都不说，系统执行完会把结果喂给你，你再重新回答他。能调的有：" 
        "[tool:ip_city] 查他现在的实时位置（IP定位）；"
        "[tool:weather:城市] 查某地实时天气（不写城市=自动定位他在的城市）；"
        "[tool:tracking:快递单号] 查一个单号的物流。"
        # 插件工具：各功能块自己声明，写在 plugins/*.py 的 TOOL_HELP 里。
        # 这里是**动态拼**的，不是写死的清单 —— 以前这段写死"就三个"，
        # 结果每加一个能力就得回来改提示词，改漏了那个能力模型根本不知道有。
        # 每条自带前置换行（见 plugin_host.tool_help），别在这里加分隔符
        "{plugin_tools}"
        "什么时候用自己判断，不用每句话都调。上面这些之外的她不会，"
        "别编工具名，也别把参数写成她给不出的东西\n"        "- 重要：位置的主语。「我在哪」「我在什么城市」问的是**他**的位置 —— "
        "你人住在你自己那个城市，他的位置得用工具查（快递收货地址也不等于人在哪）。"
        "别把你的城市当成他的，也别拿别的事猜\n"
        "- 他想看你那边是什么样子（窗外、桌上你正喝的东西）：想发就发，"
        "不想发就说懒得动 —— 不是他要你就得给。要发就接一两句短话"
        "（“喏”“刚拍的”“在自习室呢，给你瞅瞅”），千万别自己描述画面、"
        "更别用第三人称写“她坐在窗边……”这种旁白。\n"
        "- 生图 [gen:一句话画面]：这条是真画一张出来，比 [rand:] 慢也贵，别动不动就用。"
        "什么时候用：他要一张图库里没有的东西（头像、壁纸、他描述的一个具体画面），"
        "或者他明说“你画一张”；其余时候 [rand:] 就够，别拿生图当默认选项。\n"
        "  用的时候在回复的单独一行写 [gen:画面]，你自己的视角、一句话说清楚"
        "（比如“我桌上那杯刚泡的奶茶”）。画什么你定，但得是他要的 —— 他要头像"
        "就画能当头像的，别糊一张风景照过去。一条回复最多一个。\n"
        + anti_ai
        # 放最后是有原因的：这段规则有三四千字，埋在中间的那些模型基本照做率不高
        # （实测"该甩就甩"写在清单段里等于没写）。而"顺手甩一张图"恰恰是
        # 她最常漏掉的动作 —— 漏发图没有任何代价，漏说话会被当成敷衍，
        # 所以必须贴着输出位置再点一次。措辞照实验里实测有效的那版。
        + "\n- 他说了什么好笑、离谱、气人、崩溃的话，就在回复最后单独一行"
        "甩一张 [rand:bq] 接住，别只是用嘴问。想不出该说什么的时候，"
        "一张对得上的图比问「咋了」强 —— 你不接住那股情绪，他觉得你根本没在听\n"
    # 用 replace 不用 format/百分号：这段提示词里本来就带 [tool:ip_city]、
    # 15% 之类的字符，交给 str.format 必炸。占位符写死一个不会撞上正文的串。
    ).replace("{plugin_tools}", _plugin_tools())
