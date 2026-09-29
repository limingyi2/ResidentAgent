# -*- coding: utf-8 -*-
"""角色 · 云端大脑服务。

它对外提供什么
--------------
1. **远程大脑 API**（start_remote_api，端口 8788）：手机 App 与 Windows 桌宠都连它
   聊天。记忆 / 人设 / 上下文只有云上这一份，天然同步。
2. **生活与自主行为**：生活补算（过日子 + 写日记）、朋友圈、主动搭话，都跑在这个
   进程里 —— 所以这个进程不能随便退。

关于"为什么只有一个大脑"
--------------------
她的记忆库（data/memory/）是"整文件重写"式的存档：如果同时存在两个装配了 Brain
的进程，两边各持一份内存副本，谁后保存谁就把对方刚记下的东西覆盖掉 —— 记忆会
悄悄丢，而且很难发现。文件末尾的单实例守卫就是为了防这个，不是可选项。

用法
----
    python server.py            # 直接跑（Ctrl+C 退出）
"""
import os
import re
import sys
import json
import time
import random
import socket
import struct
import base64
import hashlib
import asyncio
import threading
import http.server
import urllib.error
import urllib.parse
import urllib.request

# 云机上由计划任务（SYSTEM + 重定向到文件）启动时，stdout 编码是 GBK：
# 她回复里只要带 emoji，print 就会抛 UnicodeEncodeError，把整个处理流程
# 打断在"发送"之前 —— 表现为"她明明生成了回复但对方收不到"。这里强制
# UTF-8 + errors=replace，任何字符都不会再阻断流程。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import paths
    CFG_PATH = paths.CONFIG_PATH
    WORLD_PATH = paths.WORLD_PATH        # 她的世界设定（自拍按此刻的课/时段挑地点）
except Exception:
    CFG_PATH = os.path.join(HERE, "config", "config.json")
    WORLD_PATH = os.path.join(HERE, "config", "world.json")


# ---------------------------------------------------------------- 配置

def load_config():
    """读 config.json。读不到就用空 dict —— 后面给的是人话报错，不是栈。"""
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[大脑] 读不了 config.json：{e}", flush=True)
        return {}


api_config = load_config()

# 模型中枢 + 错误日志（2026-09-28 加）：App 要在手机上换模型、导出报错日志，
# 都走这两个模块。异常钩子装上之后，线程里静默死掉的异常也会留下一行记录。
import model_hub
import errlog
try:
    errlog.install_excepthook()
except Exception:
    pass

# 访问码：新配置放 brain_token；2026-09-29 之前的老配置放在 mobile.token 里，
# 继续认（迁移期兜底）。云端 config 是独立的一份，不跟着本地改，
# 所以这个兜底必须留着 —— 否则 tok 变成 ""，_check() 会直接放行，等于撤掉鉴权。
MOB_CFG = api_config.get("mobile") or {}
if not isinstance(MOB_CFG, dict):
    MOB_CFG = {}
BRAIN_TOKEN = str(api_config.get("brain_token") or MOB_CFG.get("token") or "")

# 视觉/素材库要用的 API 配置（vision.describe_bytes 的第二参）
# 坑（2026-09-28 修）：这里以前只拷了三个键，没拷 vision —— 结果 vision.py
# 拿不到 vision.model，一直在用代码里的默认 8B 模型，config 里配的 30B 从来
# 没生效过。改模型时"看图模型"看着改了其实没变，就是漏了这一行。
API_CFG = {k: api_config[k] for k in
           ("api_base", "api_key", "model", "vision")
           if k in api_config}

IMG_TAG_RE = re.compile(r"^\s*\[img[:：]\s*([^\]]+?)\]\s*$")
GEN_TAG_RE = re.compile(r"^\s*\[gen[:：]\s*([^\]]+?)\]\s*$", re.M)


def strip_say_marker(text):
    """剥掉回复首行学主动搭话格式带出的「说」标记。

    「说」单独成行 -> 删掉该行；「说 xxx」-> 只去掉「说」保留后面的话。
    只处理第一条非空行，后面正文里正常的「说」字不动。
    """
    lines = (text or "").splitlines()
    for i, l in enumerate(lines):
        s = l.strip()
        if not s:
            continue
        if s == "说":
            lines[i] = ""
        elif s.startswith("说 ") or s.startswith("说：") or s.startswith("说:"):
            lines[i] = s[1:].lstrip(" ：:")
        break
    return "\n".join(lines).strip()


def resolve_gen_tags(text, look=""):
    """把回复里单独成行的 [gen:描述] 真的生成一张图，替换成 [img:文件名]。

    look 是她的外貌描述（人设 appearance 字段），拼在提示词最前面保证
    自拍长相稳定。生成失败就把标签整行删掉——绝不能把标签原文发出去。
    """
    def _sub(m):
        prompt = m.group(1).strip()
        if look:
            prompt = look + "，" + prompt
        n = _cloud_gen_image(prompt)
        return ("[img:%s]" % n) if n else ""
    return GEN_TAG_RE.sub(_sub, (text or ""))


# ---------------------------------------------------------------- 自拍
# 2026-09-21 用户反馈：让她发自拍，有时她只在正文里用第三人称描述画面
# （“她坐在窗边……”）却不真发图；而且图 AI 味重（塑料假脸、手指崩）。
# 根因两条：
#   1) 老的人设规则（persona_store）明写“描述用第三人称”，模型就照字面
#      把场景描写当“话”说了；出不出图全看模型心情，不稳定。
#   2) 生图从不带 negative_prompt，全靠模型猜，瑕疵没人压。
# 改法：认出“他想要她本人的照片”就**不问模型**，直接照她此刻的时间+地点
# 现场生成一张，正文用固定短句（保证第一人称、没有 AI 腔）。
# 2026-09-22 用户要求：「把之前风格的提示词都忘掉，只要记得自拍是第一人称之类的就可以了」。
# 所以负面词只留**画质 / 畸形**这类硬问题，不再堆风格标签
# （过度美颜 / 塑料假脸 / 蜡像感 / 3D渲染 / 浓妆 这些会把模型拽离参考图 —— 与"保真优先"冲突）。
GEN_NEGATIVE = ("畸形手指，多余手指，多肢体，融合的手指，扭曲五官，不对称眼睛，"
                "模糊，低分辨率，水印，文字，签名")

# 明确指向“她本人”的说法；命中就直出图。
# 2026-09-22 补：刚给过自拍之后，"再来一张 / 再拍一张 / 换一张"这类省略说法
# 也是要（新）自拍 —— 之前没识别到，模型会翻一张旧图发出来（实测踩过）。
# 2026-09-29 修「敏感肌」：这张表以前有 "看看你"（还有 "看看你现在的"），
#   但下面是**子串**匹配 —— "看看你写的日记" 里就含 "看看你"，
#   于是"要日记"被当成"要自拍"，她直接回一句"拍了张，凑合看吧"（_SELFIE_CAPS
#   里的固定句）再甩一张图出来。用户实测截图确认。
#   现在表里只留"你本人"指向明确的说法；"看看你"这种半截话改由
#   _looks_like_selfie 末尾用整句匹配判断（整句就是"看看你"才算数）。
_SELFIE_STRONG = ("自拍", "看你的样子", "你现在的样子",
                  "你现在啥样", "你长什么样", "你长啥样", "你的照片", "你的近照",
                  "你的样子", "你的自拍", "给我看看你", "让我看看你",
                  "再来一张", "再拍一张", "再拍个", "换一张", "多拍几张")
# 说的是“她那边的东西/他在给她看”，不是要她本人 —— 命中就别当自拍。
# 2026-09-29 扩表：只要句子里在说“要某个东西”，就不该出她的照片。
#   要东西 ≠ 要人。"看看你写的日记" 能挡住，靠的就是下面这两组词 + 正则。
_SELFIE_MISS = ("手边", "窗外的", "风景", "你那边的", "什么东西", "桌子",
                "书桌", "房间", "宿舍的样子", "给你看", "给你发", "我给你",
                "你看这张", "给你瞅",
                "日记", "笔记", "作业", "聊天记录", "相册", "朋友圈",
                "截图", "歌单", "课表", "收藏", "表情包", "贴纸",
                "课本", "教材", "论文", "代码", "文档", "文件夹", "资料",
                "成绩单", "计划", "日程", "壁纸", "桌面")

# 2026-09-22 曾按用户要求做过"他要看她穿睡衣/泳装就照点名换装"。
# 2026-09-29 用户明确改方向：**不要恋爱框架，也不要她有"按需服务"的感觉**。
# 这条功能整个停用 —— 她穿什么只跟她在哪、几点、在干什么有关，不跟对方点名有关。
# 代码保留（`_OUTFIT_KEYS` 只作记录），要恢复就把下面两处判断放回来。
_OUTFIT_KEYS = ("睡衣", "泳装", "泳衣", "比基尼")
_OUTFIT_OVERRIDE_ENABLED = False


def _outfit_override(text):
    """已停用：固定返回 None（按时段/场合的正常衣柜）。"""
    if not _OUTFIT_OVERRIDE_ENABLED:
        return None
    t = text or ""
    if ("泳" in t) or ("比基尼" in t):
        return random.choice([
            "白色分体比基尼泳装", "深蓝色连体泳衣",
            "浅粉色荷叶边比基尼", "黑色简约分体泳装"])
    if "睡衣" in t:
        return random.choice([
            "浅粉色纯棉长袖睡衣套装", "淡蓝色格子睡衣",
            "奶白色宽松长袖家居裙", "米色毛绒家居服"])
    return None


# “要东西”的正则兜底：紧挨着“你”出现的这些词，一律是要东西、不是要人。
# 为什么不只靠 _SELFIE_MISS 列表：换种说法就漏（"你那篇日记""你的实验报告"），
# 正则允许中间夹两三个字（"你**写的**日记""你**昨天发的**朋友圈"），耐用得多。
_SELFIE_THING_RE = re.compile(
    r"你[^，。？！,?!]{0,4}?"
    r"(日记|笔记|报告|作业|记录|截图|相册|歌单|课表|论文|代码|文档|"
    r"计划|日程|消息|回复|收藏|表情包|贴纸|礼物|课本|教材|订单|账单)")


def _looks_like_selfie(text):
    """他是不是在要“她本人”的照片。

    2026-09-29 「敏感肌」修复，判断顺序改成四道闸门（从严到宽）：
      1) 要东西 → 直接否   （"看看你写的日记""你那个作业发我看看"）
      2) 明确指向她本人 → 是（"发自拍""让我看看你""你的近照"）
      3) 整句就是"看看你/看你" → 是（以前靠子串，现在只看整句）
      4) 短的省略说法（"再拍一张""多来两张"）→ 是
    以前 3、4 混在一起又没有长度限制，导致凡是句子带个"张"字又说"来/换"的
    都能触发拍照，这是"敏感肌"的另一半原因。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 闸门 1：要东西。先查列表，再用正则兜底。
    if any(m in t for m in _SELFIE_MISS):
        return False
    if _SELFIE_THING_RE.search(t):
        return False
    # 闸门 2：明确指向她本人
    if any(h in t for h in _SELFIE_STRONG):
        return True
    # 点名要睡衣/泳装这类照片 —— 2026-09-29 去恋爱框架时已停用：
    # 不再因为对方点名就当成"要她本人的照片"去直出图。
    if _OUTFIT_OVERRIDE_ENABLED and any(k in t for k in _OUTFIT_KEYS):
        return True
    # “发/拍/来 + 照片/图 + 你”这类（“你能发张照片吗”）
    if "你" in t and ("照片" in t or "图" in t) and any(
            v in t for v in ("发", "拍", "来", "看")):
        return True
    # 闸门 3：整句就是要看她本人（"看看你""看你""看看你吧"）。
    # 以前 "看看你" 是 _SELFIE_STRONG 里的子串，任何含它的长句都会误触发；
    # 现在要求整句去掉语气词之后正好是这个意思 —— "看看你写的日记" 过不了这关。
    if re.fullmatch(r"[给我让我]{0,2}看看?你[吧呗呀啊嘛哦哈啦咧～~!！。. ]{0,3}",
                    t):
        return True
    # 闸门 4：省略说法（"再多来两张看看""换一张"）。必须够短 ——
    # 长句里捎带一个"张"字（"我来了，这张图给你看"）不算要自拍。
    if len(t) <= 8 and "张" in t and any(
            v in t for v in ("再", "多", "换", "拍", "来")):
        return True
    return False


def _hour_seg(h):
    """小时 -> 中文时段（给她自拍配光线用）。"""
    if h < 6:
        return "深夜"
    if h < 9:
        return "清晨"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 23:
        return "晚上"
    return "深夜"


# ---------------------------------------------------------------- 她的衣柜
# 2026-09-22 用户说"衣服也不用老是穿的那些" —— 之前只会在卫衣 / 针织衫 / 睡衣
# 里打转，出几次图就腻了。按**场合**分池：在家（宿舍/床/阳台）穿居家，操场穿运动，
# 其余（上课/食堂/图书馆/后街）穿外穿；池子给足花样，再偶尔加个配饰。
_WEAR_HOME = ["松垮的棉质睡衣", "浅粉色纯棉长袖睡衣套装", "白色纯棉家居套装",
              "淡蓝色格子睡衣", "宽松的灰色卫衣搭短裤", "浅黄色带小图案的家居服",
              "奶白色宽松长袖家居裙", "米色毛绒家居服"]
_WEAR_OUT = ["米白色针织开衫搭牛仔裤", "白色一字肩雪纺衫", "浅蓝色碎花连衣裙",
             "奶白色 oversize 毛衣", "浅灰色宽松卫衣", "白T恤搭牛仔外套",
             "卡其色长风衣", "浅黄色格纹衬衫", "黑色针织长袖搭阔腿裤",
             "淡紫色连帽卫衣", "白色泡泡袖衬衫搭背带裙", "燕麦色西装外套搭白T",
             "粉色小香风外套搭直筒牛仔裤", "深蓝色牛仔背带裙",
             "浅杏色毛衣搭米色半身裙", "白色针织马甲搭衬衫"]
_WEAR_SPORT = ["浅灰色运动套装", "白色运动背心搭短裤", "浅蓝色瑜伽服",
               "黑色速干运动T恤搭运动裤"]
_WEAR_ACC = ["", "", "", "", "", "，脖子上戴着一条细银项链",
             "，锁骨处戴着珍珠项链", "，头发上别着一枚小花发夹",
             "，手腕上系了一根红绳", "，耳朵上是小小的珍珠耳钉"]


def _wear_for(place):
    """按她人在哪儿挑衣服 —— 在宿舍穿居家、在操场穿运动、其余穿外穿。"""
    p = place or ""
    if ("宿舍" in p) or ("床" in p) or ("阳台" in p):
        pool = _WEAR_HOME
    elif ("操场" in p) or ("跑道" in p):
        pool = _WEAR_SPORT
    else:
        pool = _WEAR_OUT
    return random.choice(pool) + random.choice(_WEAR_ACC)


def _selfie_prompt(wear_override=None):
    """照她此刻的时间 + 课表/日常，拼一句"自拍"画面描述。返回 (画面, 地点)。

    每次都不一样：地点来自 world.json 这一刻的课 / 作息时段（没课再随机挑个
    落脚点），光线来自当前时段，姿势表情从池子里随机 —— 连着要两张也不重样。

    2026-09-22 起**不再拼入人设外貌**：长相交给参考图（`_cloud_gen_selfie`），
    文字里一描述长相，模型就照文字自己造一张脸，反而把参考图顶掉。
    wear_override：他点名想看她穿什么（睡衣/泳装），给了就盖掉按时段的随机衣柜。
    """
    import datetime
    now = datetime.datetime.now()
    hm = now.hour * 60 + now.minute
    wd = now.isoweekday()                    # 1=周一 … 7=周日

    place, doing, wear = "宿舍", "窝着刷手机", random.choice(_WEAR_HOME)
    try:
        w = json.load(open(WORLD_PATH, encoding="utf-8"))
        sched = (w.get("schedule") or {}).get(str(wd)) or []
        hit = None
        for it in sched:
            try:
                a, b = str(it[0]).split("-")
                s = int(a[:2]) * 60 + int(a[3:])
                e = int(b[:2]) * 60 + int(b[3:])
                if s <= hm <= e:
                    hit = it
                    break
            except Exception:
                continue
        # 只挑"在哪儿 + 在干嘛"，**穿什么另外按地点挑**（见 _wear_for）——
        # 这样同一时段多拍几张，地点不变衣服也会换（2026-09-22 用户要求别老穿那几件）
        if hit:
            opts = [(str(hit[2]), "在上" + str(hit[1]))]
        elif hm < 7 * 60 + 20:
            opts = [("宿舍床上", "刚醒，赖床"),
                    ("宿舍书桌前", "一边啃面包一边翻书")]
        elif hm < 12 * 60:
            opts = [("去教学楼的路上", "抱着书赶去上课"),
                    ("宿舍书桌前", "收拾书包准备出门"),
                    ("教学楼走廊", "趁课间拍一张")]
        elif hm < 14 * 60:
            opts = [("二食堂", "刚吃完午饭"),
                    ("宿舍", "午休前窝在椅子上")]
        elif hm < 17 * 60 + 40:
            opts = [("图书馆三楼自习区", "在啃珠宝鉴定的复习资料"),
                    ("宿舍书桌前", "摊着一桌资料复习"),
                    ("空教室", "一个人自习中")]
        elif hm < 19 * 60:
            opts = [("二食堂", "在吃晚饭"),
                    ("后街的奶茶店", "买了杯奶茶坐一会儿")]
        elif hm < 23 * 60:
            opts = [("操场跑道", "晚饭后散步"),
                    ("后街的奶茶店", "在奶茶店坐一会儿"),
                    ("宿舍", "洗漱完窝在椅子上")]
        else:
            opts = [("宿舍床上", "准备睡了，窝在被窝里"),
                    ("宿舍书桌前", "开着台灯在写点东西"),
                    ("宿舍阳台", "吹着夜风透透气")]
        # 这天没课的话，偶尔换去她常去的地方，别老是同一个背景
        if not sched and random.random() < 0.4:
            opts.append((random.choice(w.get("hangouts") or ["宿舍"]),
                         "在这儿待着"))
        place, doing = random.choice(opts)
        wear = wear_override or _wear_for(place)
        # 他点名穿泳装/睡衣时，场景也得跟着合理：总不能穿着比基尼上高数
        if wear_override and ("泳" in wear_override or "比基尼" in wear_override) \
                and ("泳池" not in place and "海" not in place):
            place = random.choice(["泳池边", "水上乐园的遮阳伞下", "海边的沙滩椅上"])
            doing = "刚游完泳，头发还有点湿"
        elif wear_override and "睡衣" in wear_override \
                and not (("宿舍" in place) or ("床" in place) or ("阳台" in place)):
            place = random.choice(["宿舍床上", "宿舍书桌前", "宿舍阳台"])
            doing = "睡前窝着刷手机"
    except Exception:
        pass

    light = {"清晨": "清晨柔和的自然光", "上午": "明亮的窗光",
             "中午": "正午偏硬的自然光", "下午": "午后斜射的暖阳",
             "傍晚": "傍晚暖黄的夕阳", "晚上": "夜晚暖色的灯光",
             "深夜": "深夜昏暗的暖光"}.get(_hour_seg(now.hour), "柔和的自然光")
    pose = random.choice(["对着镜头笑，眼睛弯弯的", "微微歪头看镜头",
                          "有点没睡醒的样子，眯着眼", "托着腮看镜头",
                          "抿嘴笑，眼神有点躲", "伸手比了个剪刀手",
                          "撩了一下耳边的碎发看镜头", "低头在看手机，被叫了一声才抬头"])
    # 2026-09-22：**只留画面本身**（在哪、在干嘛、穿什么、什么姿势、什么光），
    # 风格词（手机随手拍 / 胶片质感 / 浅景深 / 皮肤纹理…）全部去掉 ——
    # 用户要求"把之前风格的提示词都忘掉"，堆风格只会把参考图的效果冲淡。
    scene = "%s，%s，穿着%s，%s，%s" % (place, doing, wear, pose, light)
    return scene, place


_SELFIE_CAPS = ["喏，刚拍的～", "刚拍了一张，你看看", "就长这样啦，别嫌丑",
                "刚随手拍的，光线不太好", "给你看，别笑我啊", "喏，就现在这样",
                "拍了张，凑合看吧"]


def _selfie_caption(place=""):
    pool = list(_SELFIE_CAPS)
    if place and random.random() < 0.5:
        pool.append("在%s拍的呢" % place)
    return random.choice(pool)


def _match_img_tag(part):
    """识别她回复里单独成行的 [img:文件名] / [img：文件名]。"""
    m = IMG_TAG_RE.match(part or "")
    return m.group(1).strip() if m else None


def _sticker_path(name):
    """文件名 -> 本地路径（stickers.py 惰性加载，出错给空串）。"""
    try:
        import stickers
        return stickers.path_of(name)
    except Exception:
        return ""

MAX_CHARS = 400        # 单条消息上限，超了就拆
API_TIMEOUT = 120      # 等她回答最多等多久


# ---------------------------------------------------------------- 自己养一个她（独立模式）

_LOCAL = {"brain": None, "lock": threading.Lock()}


def build_local_brain(verbose=True):
    """按和桌宠完全一样的方式，在本进程里装配一个 Brain。

    只在 `--standalone` 时用（部署到云服务器：那里没有桌面，也开不了桌宠）。
    本机的默认模式不调它 —— 两台进程各养一个"她"会互相覆盖记忆，
    所以同一时间只允许一个进程养她（见文件末尾的单实例守卫）。
    """
    from memory_store_v2 import get_default_store
    from brain import Brain

    mem = get_default_store()
    mode = api_config.get("chat_mode", "api")
    b = Brain(mem, api_config, mode=mode)
    if verbose:
        print(f"[大脑] 独立模式：她的脑子在本进程里（chat_mode={mode}）", flush=True)

    try:
        n = b.seed_history()
        if n and verbose:
            print(f"[大脑] 接上了上次的聊天记录（{n} 条）", flush=True)
    except Exception as e:
        if verbose:
            print(f"[大脑] 聊天记录没读回来（不影响聊天）：{e}", flush=True)

    try:
        from life_engine import LifeEngine
        eng = LifeEngine(api_config, name=(b.persona.get("name") or "角色"))
        if eng.enabled:
            b.life = eng
            if verbose:
                print("[大脑] 生活引擎就绪", flush=True)
    except Exception as e:
        if verbose:
            print(f"[大脑] 生活引擎没起来（不影响聊天）：{e}", flush=True)

    return b


def catch_up_life_local(brain):
    """独立模式下开机补一次她的生活（后台跑，不挡连接）。"""
    life = getattr(brain, "life", None)
    if life is None:
        return
    try:
        res = life.catch_up()
        if res.get("events") or res.get("diary"):
            print(f"[大脑] 补算了她 {res.get('events', 0)} 条经历、"
                  f"{res.get('diary', 0)} 篇日记", flush=True)
    except Exception as e:
        print(f"[大脑] 生活补算失败（不影响聊天）：{e}", flush=True)


def ask_with_retry(text, img_b64=None):
    """让她回一句话。

    img_b64：这一轮带的图片（base64），交给视觉模型让她"看见"。
    返回 (她的话, 模式标签, 错误信息) —— 不抛异常，调用方好写。

    2026-09-29：删掉了"把消息转给本机桌宠"那条路。
    桌宠已经变成纯客户端（本地不再有任何 HTTP 接口，mobile_server.py 已摘掉），
    那条路只剩死代码。现在 server.py **只有一种形态：本进程自带大脑** ——
    云服务器上如此，单机跑也如此（run_server 里 base 恒为 None）。
    """
    # 她就在本进程里，直接问，不走网络
    if _LOCAL["brain"] is not None:
        try:
            img_path = ""
            if img_b64:
                try:
                    import vision
                    desc = vision.describe_bytes(
                        base64.b64decode(img_b64), API_CFG)
                    if desc:
                        text = (text + " " if text else "") + \
                            f"（他发的图：{desc}）"
                except Exception as e:
                    print(f"[大脑] 独立模式看图失败（不影响回复）：{e}", flush=True)
                # 把原图存档：聊天记录里留路径，App 重新进来还能看到图
                try:
                    from paths import UPLOAD_DIR
                    d = str(UPLOAD_DIR)
                    os.makedirs(d, exist_ok=True)
                    img_path = os.path.join(
                        d, "u_" + time.strftime("%Y%m%d_%H%M%S") + ".jpg")
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(img_b64))
                except Exception as e:
                    print(f"[大脑] 图片存档失败（不影响回复）：{e}", flush=True)
            # 他要自拍：不让模型“只打字不发图 / 用第三人称写场景”，直接照她此刻
            # 的时间+地点现场生成一张，正文用第一人称短句 —— 2026-09-21 加
            try:
                if not img_b64 and _looks_like_selfie(text):
                    sp, place = _selfie_prompt(wear_override=_outfit_override(text))
                    name = _cloud_gen_selfie(sp, negative_prompt=GEN_NEGATIVE,
                                             size="768x1024")
                    if name:
                        ans = _selfie_caption(place) + "\n[img:" + name + "]"
                        try:
                            _LOCAL["brain"]._log_turn(text, ans, img=None)
                        except Exception as e:
                            print(f"[大脑] 自拍存档失败（不影响回复）：{e}",
                                  flush=True)
                        return ans, "本地自拍", ""
            except Exception as e:
                print(f"[大脑] 自拍生成失败，回退正常对话：{e}", flush=True)
            with _LOCAL["lock"]:
                ans, mode = _LOCAL["brain"].chat(text, img=img_path or None,
                                                 log=False)
            # 剥掉万一学出来的「说」标记；[gen:xxx] 现场生成真图换成 [img:名字]
            look = ""
            try:
                look = str((_LOCAL["brain"].persona or {}).get("appearance")
                           or "")
            except Exception:
                look = ""
            ans = resolve_gen_tags(strip_say_marker(ans), look)
            # 存档记的是处理后的正文（带 [img:gen_xxx.png]，App 能直接显示）
            try:
                _LOCAL["brain"]._log_turn(text, ans, img=img_path or None)
            except Exception as e:
                print(f"[大脑] 存档失败（不影响回复）：{e}", flush=True)
            return ans, mode, ""
        except Exception as e:
            return "", "", f"{type(e).__name__}: {e}"

    # 理论上到不了这里（run_server 一定会先装配大脑），但别让调用方拿到 None
    return "", "", "大脑没起来"


# ---------------------------------------------------------------- 发消息用的小工具

_SENT_END = "。！？!?…～~"
_SOFT_END = "，、,；;：: "


def split_text(text, limit=MAX_CHARS):
    """把长回复拆成几条，每条不超过 limit。

    为什么不用"先按标点切再合并"：那样处理"一大段没有句号的话"会切出超长段。
    这里每次只在**前 limit 个字符里**找断点，找不到满意的就硬断 —— 长度有保证。

    断点优先级：句末标点 > 逗号/分号/空格 > 硬断。
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
    """把回复拆成"像真人连发的几条短消息"。

    她现在被要求自己用换行分条（想说什么就一行一条）；这里尊重她的
    分条，只在单行仍超长时才用 split_text 兜底硬拆。
    """
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    out = []
    for ln in lines:
        if len(ln) <= limit:
            out.append(ln)
        else:
            out.extend(split_text(ln, limit))
    return out



def _query_param(path, key):
    from urllib.parse import urlparse, parse_qs
    try:
        q = parse_qs(urlparse(path).query)
        return (q.get(key) or [""])[0]
    except Exception:
        return ""


def _cloud_diary_list():
    """云机 journal 目录里的日记日期列表（YYYY-MM-DD，升序）。"""
    import re
    try:
        from paths import JOURNAL_DIR
        d = str(JOURNAL_DIR)
    except Exception:
        d = r"C:\linzhixia\data\journal"
    try:
        return sorted(n[:-3] for n in os.listdir(d)
                      if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", n))
    except Exception:
        return []


PROACTIVE_RULES = """先看清楚现在几点、再看看要不要主动找他聊两句。

判断依据：
- 真遇到想分享的事、突然想起他、或者单纯想他了，才主动
- 真人不会每小时都主动找人，大多数时候什么都不说
- 先看一眼你们最近的对话：同一件事你已经问过、催过或说过的，绝不再提第二遍。
  哪怕他一直没回也先放着——真人不会追着人重复问同一句，想聊就换个别的话题，
  或者干脆这轮"无"
- 看一眼对话里每条前面的方括号时间：要是那句"约定的那个周末"是今早甚至更早
  说的，那都是过去说过的话，不是此刻正在谈的事 —— 别再翻出来催，也别把
  已经过去的日子当成还没到（今天几号，看最上面那句当前时间）。
- 你们之间已经定好的安排（比如假期谁去找谁、票和酒店谁负责），就按定好的说；
  记忆里写的是"已定"就别再当"没准话"来催，拿不准方向就干脆不提这件事，
  绝不能自己换个方向重问、更不能编没人说过的细节（比如"谁说票价要涨"）

输出格式（严格二选一）：
不说 -> 只输出一个字：无
想说 -> 第一行只写一个字：说
        第二行开始就是你发给他的话（一两句，口语，别写作文）

除这两种内容外，不要输出任何别的话。"""


# ---------- 主动搭话：配置 / 静默时段 / 每日限额 ----------
# 2026-09-29：以前 _proactive_loop 里是写死的 time.sleep(2400)，config.json 的
# proactive 段（enabled / max_per_day / interval_sec / quiet_start / quiet_end）
# **一个都没读** —— 用户明明设了静默时段，照样凌晨 4 点起被连发消息（实测
# 04:17~10:58 每 40 分钟一条，共 8 条）。下面这几个函数就是把它接上。

def _proactive_conf():
    """读 config.json 里的 proactive 段。读不到就返回空 dict（走代码默认值）。"""
    try:
        from paths import CONFIG_DIR
        p = os.path.join(str(CONFIG_DIR), "config.json")
    except Exception:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "config", "config.json")
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    pa = cfg.get("proactive")
    return pa if isinstance(pa, dict) else {}


def _parse_hhmm(s):
    """'23:00' / '7:30' / '24:00' → 当天第几分钟；不合法返回 None。

    24:00 合法（=1440，用来收一天的尾巴）；24:01 这类不合法 ——
    用户原来填的 quiet_start=24:00 / quiet_end=24:01 就是后者，
    解析不出来等于没设静默（这正是旧代码"照发不误"的另一个放行口）。
    """
    m = re.match(r"^(\d{1,2}):(\d{2})$", str(s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59 or (h == 24 and mi != 0):
        return None
    return h * 60 + mi


def _in_quiet(start, end, cur=None):
    """现在是否处于静默时段（这期间不主动搭话）。支持跨零点，如 23:00~07:00。

    cur 传"当天第几分钟"可脱离真实时间测试（不传就取当前时刻）。
    起止任一解析不出来、或两者相等，一律当"没设静默"（返回 False）——
    与其猜错把她整天闷住，不如照常说话，用户改配置即可。
    """
    a, c = _parse_hhmm(start), _parse_hhmm(end)
    if a is None or c is None or a == c:
        return False
    if cur is None:
        lt = time.localtime()
        cur = lt.tm_hour * 60 + lt.tm_min
    if a < c:
        return a <= cur < c          # 同日区间，如 13:00~14:00
    return cur >= a or cur < c       # 跨零点区间，如 23:00~07:00


def _proactive_quota_file():
    try:
        from paths import DATA_DIR
        return os.path.join(str(DATA_DIR), "proactive_quota.json")
    except Exception:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "proactive_quota.json")


def _proactive_used_today():
    """今天已经主动搭话几次（跨天自动归零）。"""
    try:
        d = json.load(open(_proactive_quota_file(), encoding="utf-8"))
    except Exception:
        return 0
    if str(d.get("date") or "") != time.strftime("%Y-%m-%d"):
        return 0
    try:
        return int(d.get("n") or 0)
    except Exception:
        return 0


def _proactive_count_up():
    """主动搭话计数 +1 并落盘（重启不丢）。写不进去也不能影响聊天。"""
    try:
        p = _proactive_quota_file()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"date": time.strftime("%Y-%m-%d"),
                       "n": _proactive_used_today() + 1}, f)
    except Exception:
        pass


def _cloud_append_assistant(text):
    """往聊天存档追加一条她说的话（主动搭话用）。"""
    try:
        from paths import CHAT_LOG
        p = str(CHAT_LOG)
    except Exception:
        p = r"C:\linzhixia\data\chat_history.jsonl"
    row = {"t": time.strftime("%Y-%m-%d %H:%M"), "role": "assistant",
           "text": text}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _cloud_history(limit=60):
    """最近 N 条聊天记录（t / role / text），给手机 App 显示会话用。"""
    try:
        from paths import CHAT_HISTORY
        p = str(CHAT_HISTORY)
    except Exception:
        p = r"C:\linzhixia\data\chat_history.jsonl"
    items = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                img = d.get("img") or ""
                if img:
                    img = os.path.basename(img.replace("\\", "/"))
                items.append({"t": d.get("t") or "", "role": d.get("role") or "",
                              "text": d.get("text") or "",
                              "img": img})
    except OSError:
        return []
    return items[-int(limit or 60):]


def _cloud_gen_image(prompt, model=None, negative_prompt=None, size=None,
                     image=None):
    """用硅基流动生一张图，存进 upload 目录，返回文件名。

    negative_prompt：压"AI味"的关键（畸形手指 / 塑料假脸 / 糊）。默认给
    GEN_NEGATIVE，显式传 "" 可关掉。size：自拍用 768x1024 竖图更像手机随手拍；
    哪个尺寸不认就自动退回 1024x1024 —— 别让一张图因为尺寸参数整个挂掉。
    image：参考图，**data URI 字符串**（2026-09-22 实测：传数组会报
    "image should be a string"）。传了就是图生图/图片编辑，用来锁住她的长相，
    见 `_cloud_gen_selfie`。这条路不通会自动丢掉图重试，别让一张脸把整张照片搭进去。
    """
    import base64 as _b64
    import urllib.request as _u

    try:
        cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    except Exception:
        cfg = {}
    # 生图模型：App「模型」页选的那个优先，没选过就用下面这个默认
    model = model or cfg.get("image_model") or "Tongyi-MAI/Z-Image-Turbo"
    neg = GEN_NEGATIVE if negative_prompt is None else negative_prompt
    size = size or "1024x1024"
    key = str(cfg.get("api_key") or "")
    base = str(cfg.get("api_base") or "https://api.siliconflow.cn/v1").rstrip("/")
    if not key:
        return None

    def _try(sz, img=None):
        payload = {"model": model, "prompt": prompt, "image_size": sz}
        if neg:
            payload["negative_prompt"] = neg
        if img:
            payload["image"] = img
        body = json.dumps(payload).encode("utf-8")
        req = _u.Request(base + "/images/generations", data=body, method="POST",
                         headers={"Authorization": "Bearer " + key,
                                  "Content-Type": "application/json"})
        try:
            op = _u.build_opener(_u.ProxyHandler({}))
            return json.loads(op.open(req, timeout=180).read().decode("utf-8")), op
        except Exception as e:
            print(f"[大脑] 生图失败（{sz}）：{str(e)[:80]}", flush=True)
            return None, None

    d, op = _try(size, image)
    if d is None and image:                        # 参考图这条路不通：丢掉图再试
        d, op = _try(size, None)
    if d is None and size != "1024x1024":          # 尺寸不认就退回方形再来
        d, op = _try("1024x1024")
    if d is None:
        return None
    url = ""
    try:
        url = (d.get("images") or [{}])[0].get("url") or ""
    except Exception:
        url = ""
    if not url:
        b64 = ""
        try:
            b64 = (d.get("images") or [{}])[0].get("b64_json") or ""
        except Exception:
            b64 = ""
        if not b64:
            return None
        blob = _b64.b64decode(b64)
    else:
        try:
            blob = op.open(url, timeout=120).read()
        except Exception:
            return None
    try:
        from paths import UPLOAD_DIR
        d = str(UPLOAD_DIR)
    except Exception:
        d = r"C:\linzhixia\data\upload"
    os.makedirs(d, exist_ok=True)
    name = "gen_" + time.strftime("%Y%m%d_%H%M%S") + ".png"
    with open(os.path.join(d, name), "wb") as f:
        f.write(blob)
    return name


# ---------------------------------------------------------------- 她的标准长相
# 2026-09-22 用户发来她本人的照片，说"她就长这样了"。在这之前生图只用 appearance
# 那段**文字描述**，模型每张都自己现造一张脸 —— 这就是"自拍不像她"的根。
#
# 同日实测（云端真跑，结论都验过）：
#   * Z-Image-Turbo 的 /images/generations **能吃 image（data URI）**，把她的照片
#     当参考图、配一句"保持这张脸不变"，长相就钉住了，而且只要 4~30 秒；
#   * 同指令下 Qwen-Image-Edit-2509 要 84~97 秒，脸还更"标准化"（像影楼模特），弃用；
#   * 参考图**只给脸**（上半身裁切）最好 —— 整张沙滩照会把海景一起带进结果；
#   * 提示词里**绝对不要描写长相**：一描述，模型就照文字自己造脸，参考图等于白喂
#     （第一轮就是这么把脸换掉的）。
_LOOK_CACHE = {"path": "", "uri": None}


def _look_ref_path():
    """她标准长相参考图的路径。找不到返回空串（生图不能被它带崩）。"""
    cands = []
    try:
        from paths import CONFIG_DIR, ROOT
        cands += [os.path.join(str(CONFIG_DIR), "look_refs"),
                  os.path.join(str(ROOT), "look_refs")]
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cands += [os.path.join(here, "look_refs"),
              os.path.join(here, "config", "look_refs"),
              r"C:\linzhixia\look_refs"]
    for d in cands:
        # prepared/ 是本机整理参考图时的暂存子目录，正式放法是与 look_refs 平铺
        # （云端就是平铺），这里多认一层，免得两种放法有一种读不到。
        for base in (d, os.path.join(d, "prepared")):
            if not os.path.isdir(base):
                continue
            for pref in ("look_face", "look_half", "look_full"):   # 先要"只有脸"那张
                for ext in (".jpg", ".jpeg", ".png"):
                    p = os.path.join(base, pref + ext)
                    if os.path.isfile(p):
                        return p
            for n in sorted(os.listdir(base)):                     # 兜底：任何 look_*
                p = os.path.join(base, n)
                if n.lower().startswith("look_") and os.path.isfile(p):
                    return p
    return ""


def _look_ref_uri():
    """参考图的 data URI（读一次缓存住 —— 别每张照片都重读盘 + 重新编码）。"""
    if _LOOK_CACHE["uri"]:
        return _LOOK_CACHE["uri"]
    try:
        import base64 as _b64
        p = _look_ref_path()
        if not p:
            print("[大脑] 没找到她的长相参考图（look_refs/），自拍只能退回文字描述",
                  flush=True)
            return None
        mime = "image/png" if p.lower().endswith(".png") else "image/jpeg"
        with open(p, "rb") as f:
            uri = "data:%s;base64,%s" % (mime, _b64.b64encode(f.read()).decode())
        _LOOK_CACHE["path"] = p
        _LOOK_CACHE["uri"] = uri
        print("[大脑] 已加载她的长相参考图：" + os.path.basename(p), flush=True)
        return uri
    except Exception as e:
        print(f"[大脑] 长相参考图读取失败：{str(e)[:80]}", flush=True)
        return None


# 锁脸这句有两个要点，都是 2026-09-22 拿真图对出来的：
#   1) 必须是"照参考图本人画"。写成"生成一个像照片里这样的女孩"就废了 ——
#      模型会重新画一个（第一轮实测就是这么把脸换掉的）。
#   2) 只留**长相本身**的锚点（脸小 / 眼睛大 / 皮肤白 / 甜美），
#      **不再写发型细节、也不再写任何风格词**（胶片质感、浅景深、手机随手拍…）——
#      用户要求"把之前风格的提示词都忘掉"，风格词会让模型偏离参考图。
#      发型交给参考图，别用文字去规定（两次拍摄的发型本来就不一样）。
_LOOK_LOCK = ("照参考图里这个女孩本人画，就是她本人：脸小、眼睛大而清亮、"
              "皮肤白皙、长相甜美，长相和发型都跟参考图保持一致，不要换脸。")


def _cloud_gen_selfie(scene, size="768x1024", negative_prompt=None):
    """给她**本人**的照片：先用参考图锁脸，行不通再退回纯文字描述。

    scene 只写画面（在哪、在干嘛、穿什么、什么光线），**不要写长相**。
    """
    ref = _look_ref_uri()
    if ref:
        n = _cloud_gen_image(_LOOK_LOCK + scene, negative_prompt=negative_prompt,
                             size=size, image=ref)
        if n:
            return n
    look = _her_look()
    return _cloud_gen_image(((look + "，" + scene) if look else scene),
                            negative_prompt=negative_prompt, size=size)


def _cloud_sticker(name):
    """表情包 / 聊天存档图片的二进制（App 要显示图）。找不到返回 None。

    name 既可以是表情包库文件名，也可以是聊天记录里存的图片路径
    （绝对路径，或 data 目录下的相对路径）。
    """
    try:
        p = _sticker_path(name)
        if p and os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    try:
        from paths import DATA_DIR
        data_root = os.path.abspath(str(DATA_DIR))
        seen = set()
        for c in (name, os.path.basename(name)):
            if not c or c in seen:
                continue
            seen.add(c)
            cands = [c if os.path.isabs(c) else os.path.join(data_root, c),
                     os.path.join(data_root, "upload", c)]
            for cp in cands:
                cand = os.path.abspath(cp)
                if cand.startswith(data_root) and os.path.isfile(cand):
                    with open(cand, "rb") as f:
                        return f.read()
    except Exception:
        pass
    return None


def _cloud_diary_read(date_str):
    """读一篇日记的正文（剥掉存盘时的日期标题行），没有就返回空串。"""
    if not date_str or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        return ""
    try:
        from paths import JOURNAL_DIR
        d = str(JOURNAL_DIR)
    except Exception:
        d = r"C:\linzhixia\data\journal"
    p = os.path.join(d, date_str + ".md")
    try:
        raw = open(p, encoding="utf-8").read()
    except OSError:
        return ""
    lines = raw.splitlines()
    if lines and lines[0].startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def start_remote_api(brain):
    """给"远程桌宠"开的门：本地桌宠变成薄客户端，经 SSH 隧道连这个
    API 用云上的大脑聊天 —— 记忆/人设/生活只有云上一份，天然同步。

    只绑 127.0.0.1（公网碰不到），凭 token 鉴权。本进程养着她时才启动
    —— 同一时间只允许一个进程养她，否则两边会互相覆盖记忆。
    """
    tok = BRAIN_TOKEN
    port = 8788

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _check(self):
            if not tok:
                return True
            if (self.headers.get("Authorization") or "") == "Bearer " + tok:
                return True
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                return (q.get("token") or [""])[0] == tok
            except Exception:
                return False

        def _json(self, code, out):
            data = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self._check():
                self._json(401, {"err": "bad token"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                body = {}
            path = self.path.split("?")[0]
            try:
                if path == "/api/chat":
                    ans, mode, err = ask_with_retry(
                        str(body.get("text") or ""), body.get("img") or None)
                    # 出错就留一行（欠费 402、模型名写错、地址变了都在这儿现形）
                    if err:
                        try:
                            errlog.warn("api/chat", f"{mode}：{err}")
                        except Exception:
                            pass
                    # 带上这条回复的时刻：手机 App 靠它显示每条消息的时间。
                    # 以前不返回，前端 addMsg 只能传空串 —— 结果"刚聊完的消息
                    # 一条时间都没有"，只有重进页面走 /api/history 才带 t。
                    self._json(200, {"reply": ans, "mode": mode, "err": err,
                                     "t": time.strftime("%Y-%m-%d %H:%M")})
                elif path == "/api/proactive":
                    with _LOCAL["lock"]:
                        ans, mode = brain.speak_on_scene(
                            str(body.get("scene") or ""))
                    self._json(200, {"reply": ans, "mode": mode, "err": ""})
                elif path == "/api/reset":
                    with _LOCAL["lock"]:
                        if hasattr(brain, "reset"):
                            brain.reset()
                    self._json(200, {"ok": True})
                elif path == "/api/reload_persona":
                    with _LOCAL["lock"]:
                        if hasattr(brain, "reload_persona"):
                            brain.reload_persona()
                    self._json(200, {"ok": True})
                elif path == "/api/persona/apply":
                    # App 一键切换人设：改 config.json 的 persona_file + 热重载。
                    # key 只认 personas/ 目录下已有的文件名（防路径穿越）。
                    try:
                        import persona_store
                        key = os.path.basename(str(body.get("key") or ""))
                        if not key or not os.path.isfile(
                                persona_store.path_of(key)):
                            self._json(200, {"ok": False,
                                             "err": "没有这套人设"})
                            return
                        try:
                            from paths import CONFIG_DIR
                            cfg_path = os.path.join(str(CONFIG_DIR),
                                                    "config.json")
                        except Exception:
                            cfg_path = os.path.join(
                                os.path.dirname(os.path.abspath(__file__)),
                                "config", "config.json")
                        cfg = {}
                        try:
                            cfg = json.load(open(cfg_path, encoding="utf-8"))
                        except Exception:
                            cfg = {}
                        cfg["persona_file"] = "personas/%s.json" % key
                        with open(cfg_path, "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        # 关键坑（2026-09-22）：内存里的 api_config 必须同步改！
                        # brain.api 就是启动时 load_config() 的那份同一字典，
                        # reload_persona() 读的是内存的 persona_file——只写盘不改内存，
                        # 热重载会重载回旧人设，App 的"当前"标记也永远停在第一套。
                        api_config["persona_file"] = cfg["persona_file"]
                        with _LOCAL["lock"]:
                            brain.reload_persona()
                        her = ""
                        try:
                            her = brain.persona.get("name") or ""
                        except Exception:
                            pass
                        print(f"[大脑] 人设已切换为 {key}（{her}）",
                              flush=True)
                        self._json(200, {"ok": True, "name": her})
                    except Exception as e:
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/models/apply":
                    # App 换模型：写 config.json **并且** 同步内存。
                    # 只写盘是没用的 —— Brain 拿的是启动时 load_config() 那一份
                    # 字典，不清空重填的话换了模型她还用旧的（2026-09-22 人设切换
                    # 踩过一模一样的坑）。
                    try:
                        new_cfg, changed = model_hub.apply_cfg(api_config, body)
                        if not changed:
                            self._json(200, {"ok": True, "changed": [],
                                             "msg": "没有改动"})
                        else:
                            model_hub.save(new_cfg)
                            api_config.clear()
                            api_config.update(new_cfg)
                            try:
                                API_CFG.clear()
                                API_CFG.update(
                                    {k: api_config[k] for k in
                                     ("api_base", "api_key", "model", "vision")
                                     if k in api_config})
                            except Exception:
                                pass
                            print("[模型] " + "；".join(changed), flush=True)
                            self._json(200, {
                                "ok": True, "changed": changed,
                                "current": model_hub.current(api_config)})
                    except Exception as e:
                        errlog.log_exc("api/models/apply", e)
                        self._json(200, {"ok": False, "err": str(e)[:120]})
                elif path == "/api/logs":
                    # App 把手机上的报错报上来，跟云上的存一起（分开两个文件）
                    try:
                        items = body.get("items")
                        if not isinstance(items, list):
                            items = [body]
                        saved = 0
                        for it in items[:50]:
                            if not isinstance(it, dict):
                                continue
                            errlog.log(it.get("where") or "app",
                                       it.get("msg") or "",
                                       level=str(it.get("level") or "ERROR")[:8],
                                       src="client",
                                       trace=str(it.get("trace") or ""))
                            saved += 1
                        self._json(200, {"ok": True, "saved": saved})
                    except Exception as e:
                        errlog.log_exc("api/logs", e)
                        self._json(200, {"ok": False, "err": str(e)[:120]})
                elif path == "/api/logs/clear":
                    try:
                        src = str(body.get("src") or "server")
                        ok = errlog.clear(src) if src in ("server", "client") \
                            else False
                        self._json(200, {"ok": ok, "stats": errlog.stats()})
                    except Exception as e:
                        self._json(200, {"ok": False, "err": str(e)[:120]})
                elif path == "/api/gen":
                    # 她"拍一张照片"：prompt -> 生成图 -> 存云上，返回文件名
                    name = _cloud_gen_image(str(body.get("prompt") or "")[:400])
                    self._json(200, {"name": name, "err": "" if name else "生图失败"})
                elif path == "/api/moment/like":
                    try:
                        import moments
                        m = moments.add_comment(str(body.get("id") or ""), "__like__", "")
                        self._json(200, {"ok": m is not None})
                    except Exception as e:
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/moment/comment":
                    # 他评论她的朋友圈 -> 存下来，并让她回一句
                    # who 由 App 传（显示用，"我"/昵称），不再写死"他"
                    try:
                        import moments
                        mid = str(body.get("id") or "")
                        text = str(body.get("text") or "").strip()
                        who = str(body.get("who") or "").strip() or "我"
                        hit = moments.add_comment(mid, who, text)
                        her = ""
                        if hit:
                            her = moments.reply_to_comment(brain, hit, text)
                            if her:
                                moments.add_comment(mid, "她", her)
                        self._json(200, {"ok": hit is not None, "reply": her})
                    except Exception as e:
                        self._json(200, {"ok": False, "reply": "", "err": str(e)[:80]})
                elif path == "/api/moment_now":
                    # 手动催她"现在想想要不要发朋友圈"（验证 / 演示 / 用户点一下）。
                    # 她仍然自己决定发不发 —— 这里只是挑时间，不替她写。
                    try:
                        raw, posted = try_post_moment(brain)
                        items = []
                        if posted:
                            import moments as _m
                            items = _m.list_moments(1)
                        self._json(200, {"posted": posted,
                                         "raw": raw[:300], "items": items})
                    except Exception as e:
                        self._json(200, {"posted": False, "raw": "",
                                         "items": [], "err": str(e)[:120]})
                else:
                    self._json(404, {"err": "no such api"})
            except Exception as e:
                print(f"[大脑] 接口异常 {path}：{type(e).__name__}: {e}",
                      flush=True)
                try:
                    errlog.log_exc("api" + str(path), e)
                except Exception:
                    pass
                self._json(500, {"err": f"{type(e).__name__}: {e}"})

        def do_GET(self):
            if not self._check():
                self._json(401, {"err": "bad token"})
                return
            path = self.path.split("?")[0]
            if path == "/api/persona":
                # App 设置页的人设切换要用：当前是哪套 + 有哪些预设
                import persona_store
                name = ""
                cur = ""
                try:
                    name = brain.persona.get("name") or ""
                    cur = persona_store.key_from_config(api_config)
                except Exception:
                    pass
                presets = []
                try:
                    for key, label in persona_store.list_personas():
                        meta = {}
                        try:
                            meta = json.load(open(
                                persona_store.path_of(key), encoding="utf-8"))
                        except Exception:
                            meta = {}
                        presets.append({
                            "key": key,
                            "label": meta.get("label") or label,
                            "desc": (meta.get("desc") or "")[:60],
                        })
                except Exception:
                    pass
                self._json(200, {"name": name, "key": cur, "presets": presets})
            elif path == "/api/models":
                # App「模型」页要的三样：服务商列表 / 分类好的模型清单 / 现在用哪个。
                # force=1 = 用户点了"重新拉取"，不用缓存（换 key 之后必须这样拉一次）
                try:
                    force = str(_query_param(self.path, "force") or "") == "1"
                    cur = model_hub.current(api_config)
                    lst = model_hub.list_models(cur["api_base"],
                                                cur["api_key"], force=force)
                    self._json(200, {
                        "ok": True, "current": cur,
                        "providers": [{"key": k, "name": v["name"],
                                       "base": v["base"]}
                                      for k, v in model_hub.PROVIDERS.items()],
                        "groups": lst["groups"], "source": lst["source"],
                        "count": lst["count"], "err": lst["err"],
                    })
                except Exception as e:
                    errlog.log_exc("api/models", e)
                    self._json(200, {
                        "ok": False, "err": str(e)[:120],
                        "current": model_hub.current(api_config),
                        "providers": [{"key": k, "name": v["name"],
                                       "base": v["base"]}
                                      for k, v in model_hub.PROVIDERS.items()],
                        "groups": dict(model_hub.FALLBACK),
                        "source": "fallback", "count": 0})
            elif path == "/api/logs":
                # App「错误日志」页：src=server(云上)/client(手机)，fmt=text 是要导出
                try:
                    src = _query_param(self.path, "src") or "server"
                    if src not in ("server", "client"):
                        src = "server"
                    n = _query_param(self.path, "n") or 200
                    if str(_query_param(self.path, "fmt") or "") == "text":
                        self._json(200, {"ok": True,
                                         "text": errlog.as_text(n, src)})
                    else:
                        self._json(200, {"ok": True,
                                         "items": errlog.recent(n, src),
                                         "stats": errlog.stats()})
                except Exception as e:
                    errlog.log_exc("api/logs", e)
                    self._json(200, {"ok": False, "items": [],
                                     "err": str(e)[:120]})
            elif path == "/api/diary/list":
                days = _cloud_diary_list()
                self._json(200, {"days": days})
            elif path == "/api/diary/read":
                ds = _query_param(self.path, "date")
                self._json(200, {"body": _cloud_diary_read(ds)})
            elif path == "/api/history":
                lim = _query_param(self.path, "limit") or 60
                self._json(200, {"items": _cloud_history(lim)})
            elif path == "/api/moments":
                try:
                    import moments
                    self._json(200, {"items": moments.list_moments(30)})
                except Exception as e:
                    self._json(200, {"items": [], "err": str(e)[:80]})
            elif path == "/api/app/version":
                # App 内置更新检查：读版本文件（发布新 APK 时一起更新）
                try:
                    vf = r"C:\linzhixia\app\version.json"
                    self._json(200, json.load(open(vf, encoding="utf-8")))
                except Exception as e:
                    self._json(200, {"code": 0, "err": str(e)[:60]})
            elif path == "/api/app.apk":
                apk = r"C:\linzhixia\app\zhixia.apk"
                try:
                    blob = open(apk, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.android.package-archive")
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                except Exception as e:
                    self._json(404, {"err": str(e)[:60]})
            elif path == "/api/stickers":
                names = []
                try:
                    import stickers
                    names = list(stickers.all_names())
                except Exception:
                    names = []
                try:
                    from paths import UPLOAD_DIR
                    up = str(UPLOAD_DIR)
                    if os.path.isdir(up):
                        names += [n for n in os.listdir(up)
                                  if n.lower().endswith((".jpg", ".jpeg", ".png",
                                                         ".gif", ".webp"))]
                except Exception:
                    pass
                self._json(200, {"names": names})
            elif path == "/api/img":
                name = _query_param(self.path, "name")
                blob = _cloud_sticker(name)
                if blob is None:
                    self._json(404, {"err": "no such image"})
                    return
                ext = (os.path.splitext(name)[1].lower() or ".jpg")
                ctype = {".gif": "image/gif", ".png": "image/png",
                         ".webp": "image/webp"}.get(ext, "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            else:
                self._json(404, {"err": "no such api"})

        def log_message(self, *a):
            pass

    # 默认只绑 127.0.0.1（公网碰不到）。要让手机 App 直接连，必须配
    # `bind_host: "0.0.0.0"`（或走 Tailscale/隧道），靠 token 兜底鉴权。
    # 老配置里这个值在 mobile.remote_host，继续认（迁移期兜底）。
    host = str(api_config.get("bind_host")
               or MOB_CFG.get("remote_host") or "127.0.0.1")
    srv = http.server.ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[大脑] 远程大脑 API 就绪：{host}:{port}", flush=True)
    return srv



# 生图统一后缀：让她发的照片像"手机随手拍"，不是 AI 画
def _her_look():
    """她的人设外貌描述。**现在只是兜底**：自拍默认用参考图锁脸
    （见 `_cloud_gen_selfie`），只有参考图缺失 / 调用失败时才退回文字描述生图。
    """
    try:
        return str((_LOCAL["brain"].persona or {}).get("appearance") or "")
    except Exception:
        return ""


def _moment_img_ok(name):
    """[img:名字] 引用的图必须真存在，否则 App 会显示裂图。
    生成图存在 upload 目录，也可能是聊天里出现过的贴图/照片（走 _cloud_sticker 解析）。"""
    try:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name:
            return False
        try:
            from paths import UPLOAD_DIR
            if os.path.isfile(os.path.join(str(UPLOAD_DIR), name)):
                return True
        except Exception:
            pass
        return bool(_cloud_sticker(name))
    except Exception:
        return False


def _add_moment_from_answer(ans):
    """把模型 '发\\n正文…' 的回答解析成一条朋友圈并落盘。

    2026-09-22：用户要她"像普通女孩一样发朋友圈"（自拍 / 好吃的 / 风景 / 校园猫狗 /
    八卦…）。配图三种标签，语义不同：
      [selfie:描述] 照片里有她本人 → 走参考图锁脸（_cloud_gen_selfie）+ 竖图
      [gen:描述]    照片里没有她（吃的/风景/猫狗）→ 直接照描述生成
      [img:文件名]  引用一张**已存在**的图（她记忆里发过的照片/贴图）
    三种都带 GEN_NEGATIVE 压"AI 味"。返回 True 表示成功发了一条。

    两个坑（都是 2026-09-22 实测踩到的，别改回去）：
      1) 模型常把标签写在**正文同一行末尾**（"...改天我也想搞一个 [img:xx.jpg]"），
         不是独立一行 —— 所以必须全文正则找，**不能按整行匹配**，
         否则标签原样漏进正文、配图还丢了。
      2) 模型会"抄"历史里见过的 [img:名字]，但那个名字未必存在 —— 必须校验，
         不存在就丢掉标签，否则 App 上是裂图。
    """
    import re as _re
    import moments
    lines = [l.strip() for l in (ans or "").splitlines() if l.strip()]
    # 只有她明确以「发」开头才算要发——防止把聊天式回答当朋友圈
    if not lines or not lines[0].startswith("发"):
        return False
    body = "\n".join(lines[1:])
    imgs = []

    def _take_selfie(m):
        desc = (m.group(1) or "").strip()
        # 长相靠参考图锁（_cloud_gen_selfie），描述里只写画面；不加风格后缀
        n = _cloud_gen_selfie(desc, negative_prompt=GEN_NEGATIVE, size="768x1024")
        if n:
            imgs.append(n)
        return ""

    def _take_scene(m):
        desc = (m.group(1) or "").strip()
        n = _cloud_gen_image(desc, negative_prompt=GEN_NEGATIVE, size="768x1024")
        if n:
            imgs.append(n)
        return ""

    def _take_img(m):
        name = (m.group(1) or "").strip()
        if _moment_img_ok(name):
            imgs.append(name)
        return ""

    body = _re.sub(r"\[selfie[:：]\s*([^\]]+?)\s*\]", _take_selfie, body)
    body = _re.sub(r"\[gen[:：]\s*([^\]]+?)\s*\]", _take_scene, body)
    body = _re.sub(r"\[img[:：]\s*([^\]]+?)\s*\]", _take_img, body)
    body = body.strip()
    if not body and not imgs:
        return False
    moments.add_moment(body, imgs)
    # 2026-09-29：发完得告诉她"自己"一声 —— 追一条她自己的经历。
    # 不然她发完就忘，聊天里提不起来（用户问"她知道自己发朋友圈吗"，
    # 原来的答案就是"不知道"：moments.json 和 life 的 events 是两套东西）。
    try:
        life = getattr(_LOCAL["brain"], "life", None)
        if life is not None:
            life.note_moment(body, imgs)
    except Exception as e:
        print(f"[大脑] 朋友圈回写生活状态失败（不影响发圈）：{e}", flush=True)
    return True


def try_post_moment(b):
    """让她"想想要不要发朋友圈"，要发就发掉。返回 (她这次的原话, 是否真发了)。

    抽出来给两处共用：后台 _moment_loop（定时）和 /api/moment_now（手动催一条）。
    原话一定要返回 —— 她回"无"（决定不发）和格式跑偏，从结果上看都是"没发"，
    不把原话打出来根本分不清（2026-09-22 就是靠这个才排查出问题）。
    """
    import moments
    life = getattr(b, "life", None)
    events = life.events(days=2) if life else []
    recent = [m.get("text") for m in moments.list_moments(3)]
    prompt = moments.moment_prompt_with_events(events, recent=recent)
    # 走朋友圈专用通道（Brain.moment）：不能用 chat()，否则提示词被包成
    # "他现在对你说：…" + 最近聊天，模型会当成聊天消息只回一个"发"字（2026-09-22 踩过）
    if hasattr(b, "moment"):
        ans = b.moment(prompt)
    else:
        with _LOCAL["lock"]:
            ans, _mode = b.chat(prompt, proactive=True, log=False)
    posted = _add_moment_from_answer(ans)
    return (ans or "").strip(), posted


_SEEDED_AT = {"t": 0.0}      # 种开场圈的时刻；让 _moment_loop 的首跑别紧跟着重复发


def _seed_initial_moment(b):
    """启动时空着朋友圈太尴尬：若她一条都没有，先用今天/昨天的经历种一条，
    免得用户点开发现啥都没有。失败就静默跳过（下次循环还会再试）。"""
    import moments
    try:
        time.sleep(8)        # 等 API 稳一点再发首条
        if moments.list_moments(1):
            return
        _raw, posted = try_post_moment(b)
        if posted:
            _SEEDED_AT["t"] = time.time()
            print("[大脑] 给她种了条开场朋友圈", flush=True)
    except Exception as e:
        print(f"[大脑] 种开场朋友圈失败（忽略）：{str(e)[:80]}", flush=True)


def run_server():
    base = None
    if not base and _LOCAL["brain"] is None:
        # 云服务器 / 没开桌宠的场景：本进程自己养一个她（记忆、人设、生活全带）
        print("[大脑] 没找到桌宠 —— 切换独立模式（本进程自带大脑）", flush=True)
        _LOCAL["brain"] = build_local_brain()
    if _LOCAL["brain"] is not None:
        # 独立模式才有"远程大脑 API"——本地桌宠走 SSH 隧道来用云上的大脑
        try:
            start_remote_api(_LOCAL["brain"])
        except Exception as e:
            print(f"[大脑] 远程大脑 API 没起来（不影响主服务）：{e}",
                  flush=True)
        # 生活补算：她过日子 + 夜里写日记（云上没有桌宠定时器，靠这个补；
        # catch_up 幂等 + 自带节流，循环着跑才不会漏掉跨天）
        def _life_loop(b):
            while True:
                catch_up_life_local(b)
                time.sleep(1800)
        threading.Thread(target=_life_loop,
                         args=(_LOCAL["brain"],), daemon=True).start()

        # 历史摘要：把滑出窗口的旧对话压成"聊过什么"，让她不忘前几天。
        # 2026-09-29 加。以前 12 条窗口之外的原话是直接丢掉的，丢了不变成
        # 任何东西 —— 这就是"昨天聊的今天就忘"。现在按天压成摘要跟着走。
        # 启动 60 秒后先补一次（把积压的全压完，实测约 ¥0.03），之后每 20 分钟。
        def _recap_loop(b):
            import recap_store
            time.sleep(60)
            while True:
                try:
                    st = recap_store.tick(b.api)
                    if st.get("pressed"):
                        print("[大脑] 历史摘要：压了 %d 段 / %d 字"
                              % (st["pressed"], st.get("chars", 0)), flush=True)
                except Exception as e:
                    print(f"[大脑] 历史摘要出错（不影响聊天）：{str(e)[:80]}",
                          flush=True)
                # 约定账本：从记忆库里把"有日期的说好的事"捞出来按日期注入。
                # 跟着摘要循环一起跑就够（20 分钟一次），纯读盘、不调模型。
                try:
                    import agenda
                    agenda.refresh()
                except Exception as e:
                    print(f"[大脑] 约定账本出错（不影响聊天）：{str(e)[:80]}",
                          flush=True)
                time.sleep(1200)
        threading.Thread(target=_recap_loop,
                         args=(_LOCAL["brain"],), daemon=True).start()

        # 朋友圈：每小时让她"想想要不要发"，发不发、发什么由她定
        def _moment_loop(b):
            import moments
            first = True
            while True:
                try:
                    # 启动后先快跑一次（150s），保证"今天有东西可看"，别让用户重启完
                    # 干等一小时才等到她发圈；刚种过开场圈就跳过这次，免得两分钟内连发两条。
                    # 之后恢复每小时一次。
                    time.sleep(150 if first else 3600)
                    if first and (time.time() - _SEEDED_AT["t"]) < 1500:
                        first = False
                        continue
                    first = False
                    if moments.today_count() >= 4:
                        continue        # 一天最多四条（自拍/吃的/风景/猫狗/八卦都能发，活跃一天很正常）
                    raw, posted = try_post_moment(b)
                    if posted:
                        print("[大脑] 她发了一条朋友圈", flush=True)
                    else:
                        # 把原话打出来：她才有可能只是"这轮不想发"，也可能是格式跑偏。
                        # 只看到"没发"是没法区分这两种情况的（2026-09-22 靠这个排查）
                        print("[大脑] 朋友圈：这轮没发，她的原话＝%s"
                              % raw.replace("\n", " / ")[:140], flush=True)
                except Exception as e:
                    print(f"[大脑] 朋友圈循环出错：{str(e)[:80]}", flush=True)
        threading.Thread(target=_moment_loop,
                         args=(_LOCAL["brain"],), daemon=True).start()
        # 启动时空着朋友圈太尴尬：若一条都没有，先种一条（用今天/昨天的经历）
        threading.Thread(target=_seed_initial_moment,
                         args=(_LOCAL["brain"],), daemon=True).start()

        # 主动搭话：同样由她决定说不说；说了就写进聊天存档，
        # 手机 App 的后台轮询会拉到这条消息并弹通知
        def _proactive_loop(b):
            while True:
                try:
                    # 每轮重新读配置：改完 config.json 不用重启大脑就生效
                    pa = _proactive_conf()
                    interval = int(pa.get("interval_sec") or 2400)
                    time.sleep(max(300, interval))
                    if not pa.get("enabled", True):
                        continue
                    # 静默时段：默认 23:00~07:00。
                    # 旧代码 24 小时不停，凌晨那 8 条就是这么来的。
                    qs = pa.get("quiet_start", "23:00")
                    qe = pa.get("quiet_end", "07:00")
                    if _in_quiet(qs, qe):
                        print("[大脑] 静默时段（%s~%s），这轮不主动搭话"
                              % (qs, qe), flush=True)
                        continue
                    # 每日上限：她一天主动几十条比不说话更烦人
                    cap = int(pa.get("max_per_day") or 99)
                    used = _proactive_used_today()
                    if used >= cap:
                        print("[大脑] 今天主动搭话已到上限（%d/%d）"
                              % (used, cap), flush=True)
                        continue
                    with _LOCAL["lock"]:
                        ans, _mode = b.chat(PROACTIVE_RULES, proactive=True,
                                            log=False)
                    ans = (ans or "").strip()
                    # strip_say_marker 兼容「说」单独成行和「说 xxx」连写两种，
                    # 旧写法 lines[1:] 会把「说 我找找，你等着」的正文一起丢掉
                    text = strip_say_marker(ans)
                    if not text or text == "无":
                        continue
                    text = resolve_gen_tags(text)
                    if not text:
                        continue
                    _cloud_append_assistant(text)
                    _proactive_count_up()
                    print("[大脑] 她主动找他说话了（今天第 %d 次）"
                          % (used + 1), flush=True)
                except Exception as e:
                    print(f"[大脑] 主动搭话循环出错：{str(e)[:80]}", flush=True)
        threading.Thread(target=_proactive_loop,
                         args=(_LOCAL["brain"],), daemon=True).start()

    print("=" * 58, flush=True)
    print(" 角色 · 云端大脑", flush=True)
    print(" 远程 API / 生活 / 朋友圈 / 主动搭话 照常运行。", flush=True)
    print("=" * 58, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[大脑] 手动退出。", flush=True)
    return 0



def main():
    return run_server()


if __name__ == "__main__":
    # 单实例守卫：同一时间只允许一个大脑进程，否则两个实例会抢同一个端口。
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        mtx_name = os.environ.get(
            "BRAIN_MUTEX", "Global\\LinZhixiaBrainSingleInstance")
        mutex = kernel32.CreateMutexW(None, False, mtx_name)
        if kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            print("[大脑] 已在运行，本实例退出。", flush=True)
            sys.exit(0)
    except Exception:
        pass
    sys.exit(main())
