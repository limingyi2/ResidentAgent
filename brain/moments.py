# -*- coding: utf-8 -*-
"""她的朋友圈。

她自己决定发不发 —— 定时只是"让她想想要不要发"，不是替她写。
存 data/moments.jsonl：{id, t, text, imgs:[文件名], likes:[], comments:[{who, text}]}
"""
import json
import os
import time

try:
    from paths import DATA_DIR
    MOM_PATH = os.path.join(str(DATA_DIR), "moments.jsonl")
except Exception:
    MOM_PATH = os.path.join(r"C:\linzhixia\data", "moments.jsonl")

MOMENT_RULES = """现在是一个随机时刻。你要决定：要不要发一条朋友圈，发什么。

你是个普通女大学生，朋友圈就是你随手记生活的地方。能发的东西很多，比如：
- 自拍：今天穿了新买的衣服 / 换了发型 / 化了淡妆，想拍一张
- 好吃的：周末或节假日出去下馆子、喝奶茶、吃到一家很好吃的店
- 风景：路上好看的天空、晚霞、雨后的校园、随手拍的街景
- 校园小动物：教学楼下的小猫、操场边的狗、逗猫喂猫的瞬间
- 校园八卦：班里的、宿舍的、听来的新鲜事，吐槽两句
- 日常心情：累了、开心、emo、被夸了、买到想要的东西

判断依据：
- 有想分享的就发；一周发几条都很正常，想到就发，别憋着
- 别硬凑、别天天一个味道，真没什么可说就回"无"
- 内容换着来（自拍 / 吃的 / 风景 / 小猫 / 八卦 / 心情 轮流来），别连着发同一类
- 这是发到朋友圈的，不是回消息：不要回答任何问题、不要跟谁对话、别用第三人称说自己

配图：真人发朋友圈基本都带图，所以**能配就配一张**；其中**大约一半要拍你自己（自拍）** ——
今天穿了新买的衣服、换了发型/洗了头、化了淡妆出门、这门课困到不行、在食堂吃到好吃的，
都是发自拍的好理由，别不好意思拍。拍自己的时候，场景要跟着"现在几点、你此刻在哪"走
（宿舍 / 教室 / 食堂 / 图书馆 / 操场 / 后街 / 路上），别老是同一个地方，也别老是同一个表情。
想配图就在正文最后单独一行写一条图指令，只能用下面两种写法：
- [selfie:画面描述]  —— 拍你自己：写清此刻在哪里、穿什么、什么表情动作
- [gen:画面描述]     —— 拍别的东西（吃的 / 风景 / 小猫小狗）：描述那个画面
注意：这里一律是"现拍一张新照片"，不要写 [img:文件名] 那种引用旧图的写法。
确实不想配图就别写。

输出格式（严格二选一）：
不发 -> 只输出一个字：无
要发 -> 第一行只写一个字：发
        第二行开始是正文（1~3 句，口语，像真的在发朋友圈，别加引号）
        （可选）最后单独一行写一条配图指令

除这两种内容外，不要输出任何别的话。"""


def _holiday_note(day_str):
    """world.json 里若配了 holidays（["2026-10-01", ...]），放假当天给她点提示，
    好让她像真人一样"节假日出去吃好吃的"。没配就返回空串。"""
    try:
        import json as _json
        from paths import WORLD_PATH as _wp
        w = _json.load(open(_wp, encoding="utf-8"))
        days = w.get("holidays") or []
        if day_str in days:
            return w.get("holiday_name") or "假期"
    except Exception:
        pass
    return ""


def moment_prompt_with_events(events, now=None, recent=None):
    """决定要不要发朋友圈时，把"现在几点 + 今天经历 + 最近发过啥"都喂进去。

    没有这些，她常常"没话可说"就回'无'，朋友圈永远是空的（实测就是这个原因）；
    也容易翻来覆去发同一种（老是自拍 / 老是吐槽）。
      events：life.events(days=1) 这种列表，给她真实素材
      recent：最近几条朋友圈正文，用来避免重复
    """
    import datetime
    now = now or datetime.datetime.now()
    wd = "一二三四五六日"[now.isoweekday() - 1]
    p = MOMENT_RULES
    p += ("\n\n现在是 %s 星期%s %s。"
          % (now.strftime("%Y-%m-%d"), wd, now.strftime("%H:%M")))
    hol = _holiday_note(now.strftime("%Y-%m-%d"))
    if hol:
        p += "（今天放假：%s，可以出去玩、吃好吃的。）" % hol
    elif now.isoweekday() >= 6:
        p += "（周末，出去玩了、吃了好吃的都可以发。）"
    else:
        p += "（上学日。）"
    evs = [e for e in (events or []) if isinstance(e, dict) and e.get("text")]
    if evs:
        p += "\n\n今天你经历过的（可以从中挑一件来发，也可以发别的）：\n"
        for e in evs[-8:]:
            p += "- %s：%s\n" % (e.get("slot", ""), e["text"])
    rec = [str(t).replace("\n", " ")[:40] for t in (recent or []) if t]
    if rec:
        p += "\n\n你最近已经发过这些，别重复同样的内容和配图：\n"
        for t in rec[:3]:
            p += "- %s\n" % t
    return p


def list_moments(limit=30):
    try:
        with open(MOM_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("t") or "", reverse=True)
    return rows[:int(limit or 30)]


def add_moment(text, imgs=None):
    os.makedirs(os.path.dirname(MOM_PATH), exist_ok=True)
    row = {
        "id": str(int(time.time() * 1000)),
        "t": time.strftime("%Y-%m-%d %H:%M"),
        "text": text,
        "imgs": list(imgs or []),
        "likes": [],
        "comments": [],
    }
    with open(MOM_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def today_count():
    day = time.strftime("%Y-%m-%d")
    try:
        return sum(1 for r in list_moments(200) if (r.get("t") or "").startswith(day))
    except Exception:
        return 0


def add_comment(mid, who, text):
    try:
        rows = [json.loads(l) for l in open(MOM_PATH, encoding="utf-8") if l.strip()]
    except OSError:
        return None
    hit = None
    for r in rows:
        if r.get("id") == mid:
            r.setdefault("comments", []).append({"who": who, "text": text})
            hit = r
    if hit is None:
        return None
    with open(MOM_PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return hit


def reply_to_comment(brain, moment, comment_text):
    """她回复你给她朋友圈的评论（用她自己的口气）。

    必须走 brain.moment() 这条干净通道 —— 2026-09-22 踩过的坑：
    以前走 brain.chat()，聊天框架把提示词包成一条"他发来的消息"存进了
    聊天记录和她的对话上下文，结果 App 聊天窗里凭空多出一大段
    「你在朋友圈发了：…你怎么回他？」的绿色气泡（用户看到直接懵了），
    她的上下文也被这种提示词搅得一团糟。moment() 不碰 hist、不落聊天存档。
    """
    prompt = (f"你在朋友圈发了：{moment.get('text')}\n"
              f"他在下面评论：{comment_text}\n"
              f"回他一句。一两句话，像真人回评论：可以怼、可以得意、可以顺着聊，"
              f"别客套、别总结，就当在评论区斗嘴。")
    try:
        return (brain.moment(prompt, max_tokens=90) or "").strip()
    except Exception:
        return ""


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
