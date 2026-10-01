# -*- coding: utf-8 -*-
"""朋友圈生成流水线：让她"想想要不要发"，要发就把模型回答解析成一条朋友圈。

注意与 moments.py 的分工：moments.py 是朋友圈的存取（增删查、评论），这里只管
"生成一条新朋友圈"的决策与解析。生成必须走 Brain.moment() 专用通道，绝不能走
chat()（会被包成「他现在对你说：…」+塞聊天记录，模型有时只回一个字）。
"""
import os
import random
import time

from runtime import _LOCAL
from imggen import GEN_NEGATIVE, _cloud_gen_image, _cloud_gen_selfie, _cloud_sticker
from randimg import _cloud_random_image


def _moment_img_ok(name):
    """[img:名字] 引用的图必须真存在，否则 App 会显示裂图。
    生成图存在 upload 目录，也可能是聊天里出现过的贴图 / 照片（走 _cloud_sticker 解析）。"""
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
    """把模型 '发 / 正文…' 的回答解析成一条朋友圈并落盘。返回 True 表示真发了。

    配图三种标签，语义不同：
    [selfie:描述] 照片里有她本人 → 走参考图锁脸 + 竖图
    [gen:描述]    照片里没有她（吃的 / 风景 / 猫狗）→ 直接照描述生成
    [img:文件名]  引用一张**已存在**的图
    三种都带 GEN_NEGATIVE 压"AI 味"。

    两个坑：模型常把标签写在**正文同一行末尾**，所以必须全文正则找、不能按整行匹配，
    否则标签原样漏进正文、配图还丢了；模型还会"抄"历史里见过的文件名，而那个名字
    未必存在 —— 必须校验，否则 App 上是裂图。
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
        # 优先从随机图库拿现成的（省生成时间，也更像"随手拍下来的一张"），
        # 拉不到再按描述生成。分类从生活感的三类里随机挑
        n = ""
        try:
            import features as _feat
            if _feat.on("rand_img_moment"):
                n = _cloud_random_image(random.choice(
                    ("landscape", "acg", "general_anime")))
        except Exception:
            n = ""
        if not n:
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
    # 发完得追一条她自己的经历，否则她发完就忘、聊天里提不起来
    # （moments 和 life 的 events 本来是两套东西）
    try:
        life = getattr(_LOCAL["brain"], "life", None)
        if life is not None:
            life.note_moment(body, imgs)
    except Exception as e:
        print(f"[大脑] 朋友圈回写生活状态失败（不影响发圈）：{e}", flush=True)
    return True


def try_post_moment(b):
    """让她"想想要不要发朋友圈"，要发就发掉。返回 (她这次的原话, 是否真发了)。

    抽出来给两处共用：后台 _moment_loop 和 /api/moment_now。原话一定要返回 ——
    她回"无"和格式跑偏，从结果上看都是"没发"，不打原话根本分不清。
    """
    import moments
    life = getattr(b, "life", None)
    events = life.events(days=2) if life else []
    recent = [m.get("text") for m in moments.list_moments(3)]
    prompt = moments.moment_prompt_with_events(events, recent=recent)
    # 走朋友圈专用通道 Brain.moment：用 chat() 的话提示词会被包成
    # "他现在对你说：…" + 最近聊天，模型会当成聊天消息只回一个"发"字
    if hasattr(b, "moment"):
        ans = b.moment(prompt)
    else:
        with _LOCAL["lock"]:
            ans, _mode = b.chat(prompt, proactive=True, log=False)
    posted = _add_moment_from_answer(ans)
    return (ans or "").strip(), posted


_SEEDED_AT = {"t": 0.0}      # 种开场圈的时刻；让 _moment_loop 的首跑别紧跟着重复发


def _seed_initial_moment(b):
    """朋友圈为空时先用最近经历种一条，避免首次打开一条都没有。
    失败就静默跳过（下次循环还会再试）。"""
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
