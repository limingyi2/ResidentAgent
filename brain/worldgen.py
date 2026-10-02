# -*- coding: utf-8 -*-
"""随机生成角色的世界设定（学校 / 专业 / 课表 / 室友 …）。

以前 world.json 是一份写死的示例（海青大学 · 计算机），现在默认随机：专业从一批真实
大类里抽，课表按专业从对应课程池生成。她的大学生活严格按这份设定过（life_engine 当
硬约束），但"真实过完每一天"靠的是 life_engine 的每日补算，课表只是背景板。

用法：
python worldgen.py           覆盖写 config/world.json（旧版自动备份到 archive/）
python worldgen.py --check   只打印一份生成结果，不落盘
python worldgen.py --seed 7  固定随机种子（调试用，同一种子永远同一份世界）
"""
import os
import json
import random
import shutil
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import paths
    WORLD_PATH = paths.WORLD_PATH
    ARCHIVE_DIR = paths.ARCHIVE_DIR
except Exception:
    WORLD_PATH = os.path.join(HERE, "config", "world.json")
    ARCHIVE_DIR = os.path.join(HERE, "archive")

# --- 专业 → 课程池（真实存在的大类专业，课表里抽的就是这些课） ---
MAJORS = {
    "计算机科学与技术": ["数据结构", "操作系统", "计算机网络", "数据库原理", "软件工程", "编译原理",
                     "计算机组成原理", "算法设计", "人机交互", "离散数学", "人工智能导论",
                     "计算机图形学", "Web 开发", "Linux 系统"],
    "汉语言文学": ["中国古代文学", "中国现当代文学", "外国文学", "文学理论", "古代汉语", "现代汉语",
                 "基础写作", "语言学概论", "比较文学", "唐诗宋词研究", "文献学", "美学"],
    "临床医学": ["人体解剖学", "生理学", "生物化学", "病理学", "药理学", "诊断学", "内科学",
                 "外科学", "医学免疫学", "医学微生物学", "妇产科学", "儿科学"],
    "英语": ["综合英语", "英语听力", "英语口语", "英语写作", "英美文学", "翻译理论与实践",
             "语言学导论", "跨文化交际", "高级英语", "第二外语（日语）"],
    "会计学": ["基础会计", "中级财务会计", "高级财务会计", "审计学", "管理会计", "税法",
               "财务管理", "成本会计", "会计电算化", "经济法", "统计学"],
    "视觉传达设计": ["设计素描", "色彩构成", "图形设计", "字体设计", "版式设计", "品牌设计",
                   "UI 设计", "插画", "摄影基础", "3D 建模", "包装设计"],
    "护理学": ["基础护理学", "内科护理学", "外科护理学", "妇产科护理学", "儿科护理学",
               "急救护理学", "健康评估", "药理学", "人体解剖学", "社区护理学"],
    "经济学": ["微观经济学", "宏观经济学", "计量经济学", "国际经济学", "金融学", "财政学",
               "产业经济学", "经济史", "博弈论", "统计学"],
    "法学": ["法理学", "宪法学", "民法学", "刑法学", "行政法学", "诉讼法学", "商法",
             "国际法", "中国法制史", "知识产权法"],
    "工商管理": ["管理学原理", "市场营销", "人力资源管理", "战略管理", "运营管理",
                 "组织行为学", "创业管理", "商业数据分析"],
    "学前教育": ["学前教育学", "儿童发展心理学", "幼儿卫生学", "幼儿园课程", "游戏理论",
                 "声乐基础", "幼儿舞蹈", "美术基础", "教育心理学"],
    "数字媒体技术": ["程序设计", "数字图像处理", "三维动画", "影视后期制作", "互动媒体设计",
                   "游戏开发", "虚拟现实技术", "音频处理"],
    "心理学": ["普通心理学", "发展心理学", "社会心理学", "实验心理学", "心理统计学",
               "认知心理学", "咨询心理学", "人格心理学"],
    "环境工程": ["环境化学", "水污染控制工程", "大气污染控制工程", "环境监测",
                 "环境微生物学", "固体废物处理", "环境影响评价"],
}

CITIES = ["成都", "武汉", "西安", "南京", "杭州", "重庆", "长沙", "青岛", "厦门", "苏州",
          "昆明", "郑州", "济南", "合肥", "大连", "福州", "贵阳", "兰州"]

# 校名模板：按专业大类挑一个像样的后缀
SCHOOL_SUFFIX = {
    "师范": ["师范大学", "师范学院"],
    "医学": ["医科大学", "医学院"],
    "财经": ["财经大学", "工商大学"],
    "理工": ["理工大学", "工业大学", "科技大学"],
    "综合": ["大学", "学院"],
}


def _school_suffix(major):
    if major in ("汉语言文学", "学前教育", "心理学"):
        k = "师范"
    elif major in ("临床医学", "护理学"):
        k = "医学"
    elif major in ("会计学", "经济学", "工商管理"):
        k = "财经"
    elif major in ("计算机科学与技术", "数字媒体技术", "环境工程", "视觉传达设计"):
        k = "理工"
    else:
        k = "综合"
    return random.choice(SCHOOL_SUFFIX[k])


GIRL_NAMES = ["小满", "阿宁", "婷婷", "雯雯", "晓彤", "佳怡", "若曦", "子涵", "雨欣",
              "欣怡", "梦琪", "思琪", "可昕", "梓萱", "语桐", "糖糖", "可可", "阿May", "贝贝", "小鱼"]
ROOMIE_TRAITS = ["嗓门大、爱点外卖、考试周才突击", "安静，常泡图书馆，跟她关系最好",
                "追星族，手机壳换得比衣服勤", "健身狂，早上六点就去操场",
                "早睡早起党，十一点准时熄灯", "夜猫子，凌晨还在刷剧",
                "吃货，书包里永远有零食", "颜值党，出门前要化半小时",
                "学霸，笔记记得像印刷体", "社恐，社团结对都不去",
                "话痨，宿舍群里的活跃分子", "佛系，什么都随缘"]
CLUBS = ["摄影社", "动漫社", "文学社", "吉他社", "街舞社", "汉服社", "青年志愿者协会",
         "辩论队", "篮球社", "合唱团", "桌游社", "电影社", "滑板社", "美食社", "创业协会"]
CLUB_WHEN = ["周一晚", "周二晚", "周三晚", "周四晚", "周五晚", "周末", "双周一次"]
HANGOUTS = ["图书馆四楼", "三食堂", "学校后街", "操场", "奶茶店", "自习室", "打印店",
            "校门口地铁站", "湖边", "小花园", "创业园咖啡馆", "宿舍楼下便利店"]
BUILDINGS = ["博学楼", "文科楼", "理科楼", "阶梯教室", "艺术楼", "实验楼"]
TIME_SLOTS = ["08:00-09:40", "10:00-11:40", "14:00-15:40", "16:00-17:40"]
NOTES = [
    "在准备英语六级，有点怕考试。",
    "想找个实习，但投了几份都没回音。",
    "最近在跟室友学做手账。",
    "宿舍养了只小仓鼠，偷偷养的。",
    "在纠结要不要考研。",
    "报了个摄影比赛。",
    "最近迷上了骑行，周末常出去。",
    "在自学剪辑，想做 vlog。",
    "跟家里有点闹别扭，不想多说。",
    "选修了一门很水的课，倒是挺放松。",
]
SCHOOL_GRADE = "大三"
START_YEAR = 2024


def build_schedule(major):
    """按专业从课程池抽 6~9 门，随机排到周一~周五（周末通常空）。"""
    courses = MAJORS.get(major, MAJORS["汉语言文学"])
    chosen = random.sample(courses, k=min(len(courses), random.randint(6, 9)))
    sched = {}
    for wd in range(1, 6):                      # 周一到周五
        n = random.randint(1, 3)                # 每天 1~3 节
        slots = random.sample(TIME_SLOTS, k=min(n, len(TIME_SLOTS)))
        day = []
        pool = chosen[:]                         # 当天课名不重复
        for ts in slots:
            if not pool:
                pool = chosen[:]
            c = pool.pop(random.randrange(len(pool)))
            room = "%s %d" % (random.choice(BUILDINGS), random.randint(101, 499))
            day.append([ts, c, room])
        day.sort(key=lambda x: x[0])
        sched[str(wd)] = day
    if random.random() < 0.15:                  # 偶尔周六有课
        c = random.choice(chosen)
        sched["6"] = [[random.choice(TIME_SLOTS), c,
                       "%s %d" % (random.choice(BUILDINGS), random.randint(101, 499))]]
    else:
        sched["6"] = []
    sched["7"] = []
    return sched


def build_world(seed=None):
    """拼一份完整的 world 设定。seed 固定则同一种子同一种世界（调试用）。"""
    random.seed(seed)
    major = random.choice(list(MAJORS.keys()))
    city = random.choice(CITIES)
    school = city + _school_suffix(major)
    n_room = random.randint(1, 2)
    names = random.sample(GIRL_NAMES, k=n_room)
    traits = random.sample(ROOMIE_TRAITS, k=n_room)
    roommates = [{"name": nm, "trait": tr} for nm, tr in zip(names, traits)]
    clubs = random.sample(CLUBS, k=random.randint(1, 2))
    club_list = [{"name": c, "when": random.choice(CLUB_WHEN)} for c in clubs]
    hangouts = random.sample(HANGOUTS, k=random.randint(3, 4))
    sched = build_schedule(major)
    return {
        "_说明": (
            "这是角色自己的世界，她的生活严格按这里写的来。"
            "schedule 的键是星期（1=周一 … 7=周日），值是 [开始-结束, 课名, 地点] 的列表；"
            "roommates 里每人一个名字和一句性格；hangouts 是她常去的地方。"
            "这些是硬设定 —— 她不会编出这里没有的课、没提到的室友。"
            "这份是程序随机生成的（专业、课表按专业来），想固定就手动改这里，改完重启桌宠。"
        ),
        "school": school,
        "city": city,
        "grade": SCHOOL_GRADE,
        "major": major,
        "start_year": START_YEAR,
        "dorm": "%d 号楼 %d" % (random.randint(1, 8), random.randint(101, 699)),
        "roommates": roommates,
        "schedule": sched,
        "routine": {
            "07:20": "起床洗漱",
            "07:50": "去食堂吃早饭",
            "12:00": "午饭",
            "17:40": "晚饭",
            "23:00": "熄灯睡觉",
        },
        "hangouts": hangouts,
        "clubs": club_list,
        "notes": random.choice(NOTES),
    }


def _gen_world_model_first(seed=None):
    """优先让模型生成（本地 27B / 云端），都不成回退代码随机。"""
    try:
        from server import api_config
        from life_engine import generate_world_via_llm
        w = generate_world_via_llm(api_config, timeout=180)
        if w:
            return w
    except Exception as e:
        print("[世界] 模型生成失败：%s" % e)
    return build_world(seed)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只打印，不落盘")
    ap.add_argument("--seed", type=int, default=None, help="代码随机的种子（调试）")
    ap.add_argument("--code", action="store_true",
                    help="强制用代码随机（不走模型）。默认优先让模型生成（本地27B/云端）")
    args = ap.parse_args()

    if args.check and not args.code:
        # --check 默认也走模型，看看模型会生成啥；想看代码随机就加 --code
        w = _gen_world_model_first()
    elif args.code:
        w = build_world(args.seed)
    else:
        w = _gen_world_model_first()

    if args.check:
        print(json.dumps(w, ensure_ascii=False, indent=2))
        return

    # 落盘前备份旧 world（她以前可能手填过）
    if os.path.exists(WORLD_PATH):
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = os.path.join(ARCHIVE_DIR, "world_%s.json" % ts)
        shutil.copy2(WORLD_PATH, bak)
        print("[世界] 已备份旧 world.json -> %s" % bak)

    os.makedirs(os.path.dirname(WORLD_PATH), exist_ok=True)
    json.dump(w, open(WORLD_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("[世界] 已生成随机身份：%s · %s · %s · %s"
          % (w["city"], w["school"], w["grade"], w["major"]))
    print("[世界] ⚠️ 学校/专业变了，她以前的经历和日记会穿帮。需要清空重来：")
    print("       删掉 data/her_life/events.jsonl、data/her_life/state.json 和 "
          "data/journal/ 下的文件，重启桌宠即可。")


if __name__ == "__main__":
    main()
