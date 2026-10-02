# -*- coding: utf-8 -*-
"""回复后处理：模型吐出原文到发出去之间要过的三道加工。

顺序固定：剥「说」标记 -> [gen:描述] 现场生图 -> [rand:分类] 甩现成图
（randimg.resolve_rand_tags，在 ask_with_retry 里单独调）-> [voice] 合成语音条。
每道都是同一个原则：加工失败就降级成纯文字，不让标记原文漏给用户
"""
import re

from runtime import GEN_TAG_RE


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


def resolve_voice_tag(text, api_config):
    """把回复里的 [voice] 标记变成真语音条。

    她只在想用声音说的时候加这个标记。命中后把整段文字合成为 mp3，替换成
    `[voice:文件名]`，App 渲染成可播放的语音条。合成失败就把标记悄悄去掉、正文照发
    —— 语音是锦上添花，不能反过来害消息发不出去。
    """
    raw = (text or "")
    if "[voice]" not in raw and "[语音]" not in raw:
        return raw
    spoken = raw.replace("[voice]", "").replace("[语音]", "").strip()
    spoken = re.sub(r"\n+", " ", spoken)
    try:
        import voice
        name = voice.synth(spoken, api_config)
    except Exception as e:
        print("[语音] 调用失败：%s" % str(e)[:120], flush=True)
        name = ""
    if not name:
        return spoken
    return "[voice:%s]\n%s" % (name, spoken)


def resolve_gen_tags(text, look=""):
    """把回复里单独成行的 [gen:描述] 真的生成一张图，替换成 [img:文件名]。

    look 是她的外貌描述（人设 appearance），拼在提示词最前面保证自拍长相稳定。
    生成失败就把标签整行删掉，不让标签原文发出去
    """
    def _sub(m):
        prompt = m.group(1).strip()
        if look:
            prompt = look + "，" + prompt
        import imggen
        n = imggen._cloud_gen_image(prompt)
        return ("[img:%s]" % n) if n else ""
    return GEN_TAG_RE.sub(_sub, (text or ""))
