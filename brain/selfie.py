# -*- coding: utf-8 -*-
"""自拍：认出"他想要她本人的照片"，并按她此刻的时间/课表拼画面描述。

认出就直接生成、不问模型：让她自己决定的话，她常常只在正文里用第三人称描述
画面却不真发图。正文用固定短句，保证第一人称、没有 AI 腔。
生图本身在 imggen.py，这里只管"要不要拍"和"拍什么"。
"""
import os
import re
import json
import random

try:
    from runtime import WORLD_PATH
except Exception:
    WORLD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config", "world.json")

# 明确指向"她本人"的说法，命中就直出图。子串匹配会误伤
# （"看看你写的日记"含"看看你"），所以这张表只留指向明确的说法
_SELFIE_STRONG = ("自拍", "看你的样子", "你现在的样子",
                  "你现在啥样", "你长什么样", "你长啥样", "你的照片", "你的近照",
                  "你的样子", "你的自拍", "给我看看你", "让我看看你",
                  "再来一张", "再拍一张", "再拍个", "换一张", "多拍几张")
# 说的是"她那边的东西"或"他在给她看"，不是要她本人 —— 命中就别当自拍
_SELFIE_MISS = ("手边", "窗外的", "风景", "你那边的", "什么东西", "桌子",
                "书桌", "房间", "宿舍的样子", "给你看", "给你发", "我给你",
                "你看这张", "给你瞅",
                "日记", "笔记", "作业", "聊天记录", "相册", "朋友圈",
                "截图", "歌单", "课表", "收藏", "表情包", "贴纸",
                "课本", "教材", "论文", "代码", "文档", "文件夹", "资料",
                "成绩单", "计划", "日程", "壁纸", "桌面")

# 按需换装（他要看睡衣 / 泳装就照点名换）已停用：不要"按需服务"的感觉，
# 她穿什么只跟时间、地点、在干什么有关。要恢复就把下面两处判断放回来
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


# "要东西"的正则兜底：紧挨着"你"出现的这些词一律是要东西不是要人。
# 允许中间夹两三个字，"你那篇日记"这种换种说法也挡得住
_SELFIE_THING_RE = re.compile(
    r"你[^，。？！,?!]{0,4}?"
    r"(日记|笔记|报告|作业|记录|截图|相册|歌单|课表|论文|代码|文档|"
    r"计划|日程|消息|回复|收藏|表情包|贴纸|礼物|课本|教材|订单|账单)")


def _looks_like_selfie(text):
    """他是不是在要"她本人"的照片。

    判断顺序是四道闸门（从严到宽）：
    1) 要东西 → 直接否            2) 明确指向她本人 → 是
    3) 整句就是"看看你" → 是      4) 短的省略说法（"再拍一张"）→ 是
    3 和 4 必须分开且限制长度，否则长句里捎带一个"张"字都能触发拍照。
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
    # 点名要睡衣 / 泳装这类照片已停用：不再因为对方点名就直接出图
    if _OUTFIT_OVERRIDE_ENABLED and any(k in t for k in _OUTFIT_KEYS):
        return True
    # “发/拍/来 + 照片/图 + 你”这类（“你能发张照片吗”）
    if "你" in t and ("照片" in t or "图" in t) and any(
            v in t for v in ("发", "拍", "来", "看")):
        return True
    # 闸门 3：整句就是要看她本人。去掉语气词后要求正好是这个意思，
    # 否则"看看你写的日记"也会过关
    if re.fullmatch(r"[给我让我]{0,2}看看?你[吧呗呀啊嘛哦哈啦咧～~!！。. ]{0,3}",
                    t):
        return True
    # 闸门 4：省略说法（"再多来两张"）。必须够短，长句里捎带"张"字不算
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


# --- 她的衣柜 ---
# 按场合分池：在家穿居家、操场穿运动、其余穿外穿；池子给足花样，再偶尔加个配饰
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
    """照她此刻的时间 + 课表 / 日常，拼一句"自拍"画面描述。返回 (画面, 地点)。

    地点来自这一刻的课 / 作息时段（没课再随机挑个落脚点），姿势表情从池子里随机 ——
    连着要两张也不重样。**不拼入外貌**：长相交给参考图，文字里一描述长相，
    模型就照文字自己造一张脸，参考图等于白喂。

    wear_override：他点名想看她穿什么，给了就盖掉按时段的随机衣柜。
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
        # 只挑"在哪儿 + 在干嘛"，穿什么按地点另算（见 _wear_for），
        # 这样同一时段多拍几张，地点不变衣服也会换
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
            opts = [("图书馆三楼自习区", "在啃专业课的复习资料"),
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
        # 点名穿泳装/睡衣时场景也得跟着合理，总不能穿比基尼上高数
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
    # 只留画面本身。风格词（胶片质感 / 浅景深 / 手机随手拍…）全去掉，
    # 堆风格只会冲淡参考图的效果
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
