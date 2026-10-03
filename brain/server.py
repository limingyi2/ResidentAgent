# -*- coding: utf-8 -*-
"""云端大脑服务（组装与路由层）。

远程大脑 API（端口 8788）：手机 App 与桌宠都连它聊天，记忆/人设/上下文
只有云上这一份；生活补算、日记、朋友圈、主动搭话也都在这个进程里跑。

模块分工：runtime 共享配置｜reply 回复后处理｜randimg 随机图｜selfie 自拍｜
imggen 生图锁脸｜proactive 主动搭话｜moment_flow 朋友圈｜jobs 后台循环。
本文件只管装配大脑、聊天管线、HTTP 路由、启动。用法：python server.py
"""
import os
import sys
import json
import time
import hmac
import base64
import threading
import contextlib
import http.server

from runtime import (
    api_config, API_CFG, MOB_CFG, BRAIN_TOKEN,
    _LOCAL, _trace_write, _trace_tail, load_config, config_path, data_file)

# 模型中枢 + 错误日志：App 在手机上换模型、导出日志都走这两个模块。
# 异常钩子装上后，线程里静默死掉的异常也会留下记录
import model_hub
import errlog
try:
    errlog.install_excepthook()
except Exception:
    pass

# --- 各领域模块（server 自己用的 + 给老脚本 re-export 的都在这） ---
from reply import strip_say_marker, resolve_voice_tag, resolve_gen_tags
from randimg import resolve_rand_tags, RAND_TAG_RE, _cloud_random_image
from selfie import (_SELFIE_STRONG, _SELFIE_MISS, _SELFIE_THING_RE,
                    _looks_like_selfie, _hour_seg,
                    _wear_for, _selfie_prompt, _selfie_caption)
from imggen import (GEN_NEGATIVE, _cloud_gen_image, _cloud_gen_selfie,
                    _cloud_sticker, _her_look, _look_ref_path, _look_ref_uri,
                    _sticker_path)
from proactive import (PROACTIVE_RULES, note_client_ip, _his_ip,
                       _proactive_conf, _in_quiet, _proactive_stats_load,
                       _proactive_note_sent, _proactive_note_user,
                       _proactive_interval_multiplier, _proactive_used_today,
                       _proactive_count_up, _recent_her_texts, _norm_text,
                       _is_repeat_of_recent, _cloud_append_assistant)
from moment_flow import (try_post_moment, _SEEDED_AT, _seed_initial_moment,
                         _add_moment_from_answer, _moment_img_ok)
from jobs import catch_up_life_local, start_background

# re-export 给外部脚本用（worldgen.py 与 _test_* 仍从 server 取这些名字）
__all__ = [
    "api_config", "API_CFG", "MOB_CFG", "BRAIN_TOKEN", "load_config",
    "strip_say_marker", "resolve_voice_tag", "resolve_gen_tags",
    "resolve_rand_tags", "RAND_TAG_RE", "_cloud_random_image",
    "_SELFIE_STRONG", "_SELFIE_MISS", "_SELFIE_THING_RE",
    "_looks_like_selfie", "_hour_seg", "_wear_for", "_selfie_prompt",
    "_selfie_caption", "GEN_NEGATIVE", "_cloud_gen_image", "_cloud_gen_selfie",
    "_cloud_sticker", "_her_look", "_look_ref_path", "_look_ref_uri",
    "_sticker_path", "PROACTIVE_RULES", "note_client_ip", "_his_ip",
    "_proactive_conf", "_in_quiet", "_proactive_stats_load",
    "_proactive_note_sent", "_proactive_note_user",
    "_proactive_interval_multiplier", "_proactive_used_today",
    "_proactive_count_up", "_recent_her_texts", "_norm_text",
    "_is_repeat_of_recent", "_cloud_append_assistant", "try_post_moment",
    "_SEEDED_AT", "_seed_initial_moment", "_add_moment_from_answer",
    "_moment_img_ok", "catch_up_life_local", "start_background",
    "build_local_brain", "ask_with_retry", "start_remote_api", "run_server",
]


# --- 自己养一个她（独立模式） ---

def build_local_brain(verbose=True):
    """按和桌宠完全一样的方式，在本进程里装配一个 Brain。

    只在 --standalone 时用（云端没有桌面，开不了桌宠）。两台进程各养一个"她"
    会互相覆盖记忆，所以同一时间只允许一个进程养她。
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
        eng = LifeEngine(api_config, name=(b.persona.get("name") or ""))
        if eng.enabled:
            b.life = eng
            if verbose:
                print("[大脑] 生活引擎就绪", flush=True)
    except Exception as e:
        if verbose:
            print(f"[大脑] 生活引擎没起来（不影响聊天）：{e}", flush=True)

    return b


# 聊天锁的等待上限（秒），算出来的不是拍的：
#   一轮 chat 最坏 = 60s + 重试间隔 1.5s + 重试 60s = 121.5s
#   工具轮最多两次 chat = 243s
#   UAPI 调用最坏 = 6s × 2 = 12s
#   合计约 255s，图片/语音的合成在锁外。取 300 留余量。
# 超了明说"在忙"。以前无超时地挂着，一个卡住的请求能把后面全堵死。
CHAT_LOCK_WAIT = 300

# 单次请求体上限。带图 base64 也就几 MB，12MB 够用，
# 顺便挡掉"报个超大 Content-Length 把内存打满"。
MAX_BODY = 12 * 1024 * 1024


class _Busy(Exception):
    """等聊天锁超时：这一轮没轮到脑子。"""


@contextlib.contextmanager
def _chat_lock(timeout=CHAT_LOCK_WAIT):
    """有上限地拿聊天锁（超时抛 _Busy，不无限期阻塞）。"""
    lock = _LOCAL["lock"]
    if not lock.acquire(timeout=timeout):
        raise _Busy("等聊天锁超过 %d 秒" % timeout)
    try:
        yield
    finally:
        lock.release()


def _is_loopback(ip):
    """是不是本机回环地址（含 ::1）。"""
    s = str(ip or "").strip().strip("[]").lower()
    return s in ("127.0.0.1", "::1", "localhost") or s.startswith("127.")


def _auth_ok(auth_header, query_token, config_token, peer_ip):
    """鉴权判定（纯函数，方便直接测）。

    - 配了 token：Authorization 头优先，?token= 兼容老客户端（<img>/<audio>
      设不了头，只能走 query）；用 compare_digest 比，`==` 会在第一个不同的
      字符上短路，长度和前缀可以被计时探测出来。
    - **没配 token：只放行本机回环**。不能"没配就全放行"（fail-open），
      配上 bind_host: 0.0.0.0 就等于把整个 API 摆到公网 —— 而忘记填 token
      恰恰是最常见的情况。
    """
    if not config_token:
        return _is_loopback(peer_ip)
    got = str(auth_header or "")
    if got.startswith("Bearer "):
        got = got[7:]
    if not got:
        got = str(query_token or "")
    if not got:
        return False
    try:
        return hmac.compare_digest(got, str(config_token))
    except Exception:
        return False


def _snapshot(b):
    """把这一轮的 trace 字段从大脑上抄下来。

    **必须在持锁时调用**：last_sizes / last_hits / last_ms / last_usage 都长在
    共享的 Brain / MemoryStore 上，出锁再读会被并发那一轮覆盖。
    """
    hits = getattr(getattr(b, "mem", None), "last_hits", None) or []
    usage = getattr(b, "last_usage", None) or {}
    return {
        "blocks": getattr(b, "last_sizes", None) or {},
        "mem_hits": len(hits),
        "mem_top": [round(float(s), 2) for s, _t in hits[:3]],
        "ms": getattr(b, "last_ms", None),
        "tok_in": usage.get("in"), "tok_out": usage.get("out"),
        "model": str((API_CFG or {}).get("model") or ""),
    }


def _chat_with_tools(text, img_path, trace_out=None):
    """聊一轮，带模型自选工具：她写了 [tool:名] 就执行并喂回结果让她重答。

    开关 tools 关了就走普通聊天。锁在每轮 chat 里拿 —— 工具查询本身不碰
    大脑，别抱着锁去等 UAPI 的网络超时。
    """
    def _once(t):
        with _chat_lock():
            b = _LOCAL["brain"]
            ans, mode = b.chat(t, img=img_path, log=False)
            # trace 字段长在共享的大脑对象上，得趁锁还在手里抄。出了锁再读，
            # 并发那一轮会把它覆盖掉。
            # 每轮都覆盖：工具调用走两轮 chat，用户看到的是最后一轮的结果。
            if trace_out is not None:
                trace_out.update(_snapshot(b))
            return ans, mode

    try:
        import features as _feat
        if not _feat.on("tools"):
            # 开关关了不执行查询，但 prompt 是拼好的，模型仍可能写工具标签，
            # 照样清扫一遍
            import tools as _tools
            ans, mode = _once(text)
            return _tools.TOOL_TAG_ANY_RE.sub("", ans).strip(), mode
    except _Busy:
        raise
    except Exception:
        pass
    import tools as _tools
    ans, mode, used = _tools.run_chat_with_tools(_once, text)
    if used:
        print(f"[大脑] 她调用了工具 {used}", flush=True)
        # 用过的工具要进 trace，否则重启后没法追"这轮为什么答成那样"
        if trace_out is not None:
            trace_out["tool"] = used
    return ans, mode


def ask_with_retry(text, img_b64=None, display=None, trace_out=None):
    """让她回一句话。

    img_b64：这一轮带的图片（base64），交给视觉模型让她"看见"。
    display：存档/给用户看的那份原文（不含系统注入的提示），None 就用 text。
    trace_out：可选 dict；填上就在**持锁时**抄下这轮的 trace 字段（见 _snapshot）。
    返回 (她的话, 模式标签, 错误信息) —— 不抛异常，调用方好写。

    server.py 只有一种形态：本进程自带大脑，不再转发给桌宠。
    """
    orig_text = text if display is None else display
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
            # 他要自拍：不指望模型"只打字不发图"，直接按她此刻的时间和地点
            # 现场生成一张，正文用第一人称短句
            try:
                if not img_b64 and _looks_like_selfie(text):
                    sp, place = _selfie_prompt()
                    name = _cloud_gen_selfie(sp, negative_prompt=GEN_NEGATIVE,
                                             size="768x1024")
                    if name:
                        ans = _selfie_caption(place) + "\n[img:" + name + "]"
                        try:
                            _LOCAL["brain"]._log_turn(orig_text, ans, img=None)
                        except Exception as e:
                            print(f"[大脑] 自拍存档失败（不影响回复）：{e}",
                                  flush=True)
                        return ans, "本地自拍", ""
            except Exception as e:
                print(f"[大脑] 自拍生成失败，回退正常对话：{e}", flush=True)
            # 锁在 _chat_with_tools 的每轮 chat 里拿（带超时），别抱着锁等网络
            ans, mode = _chat_with_tools(text, img_path or None,
                                         trace_out=trace_out)
            # 剥掉学出来的「说」标记；[gen:xxx] 换成 [img:名字]
            look = _her_look()
            ans = resolve_gen_tags(strip_say_marker(ans), look)
            ans = resolve_rand_tags(ans, text)   # [rand:分类] 表情包/趣图
            ans = resolve_voice_tag(ans, API_CFG)
            # 存档记处理后的正文（带 [img:gen_xxx.png]，App 能直接显示）。
            # 用户侧只存他真正说的话，看图描述和快递提示别进他的气泡
            try:
                _LOCAL["brain"]._log_turn(orig_text, ans, img=img_path or None)
            except Exception as e:
                print(f"[大脑] 存档失败（不影响回复）：{e}", flush=True)
            return ans, mode, ""
        except _Busy:
            # 排队超时：明确说出来，别让调用方无限期等
            return "", "忙碌", "她正在回上一条，几秒后再发一次就好"
        except Exception as e:
            return "", "", f"{type(e).__name__}: {e}"

    # 理论上到不了这里（run_server 一定会先装配大脑），但别让调用方拿到 None
    return "", "", "大脑没起来"


# split_text / split_messages 搬进了 brain.py，/api/chat 的 messages 和存档共用一份
from brain import split_messages


def _query_param(path, key):
    from urllib.parse import urlparse, parse_qs
    try:
        q = parse_qs(urlparse(path).query)
        return (q.get(key) or [""])[0]
    except Exception:
        return ""


def _stats_today():
    """今天的用量汇总（App 设置页显示"今天聊了多少轮、花了多少 token"）。"""
    today = time.strftime("%Y-%m-%d")
    rows = [r for r in _trace_tail(4000) if str(r.get("t", "")).startswith(today)]
    tok_in = sum(int(r.get("tok_in") or 0) for r in rows)
    tok_out = sum(int(r.get("tok_out") or 0) for r in rows)
    ms = [int(r.get("ms") or 0) for r in rows if r.get("ms")]
    mem_hit = sum(1 for r in rows if int(r.get("mem_hits") or 0) > 0)
    out = {
        "date": today,
        "turns": len(rows),
        "tok_in": tok_in,
        "tok_out": tok_out,
        "fallback": sum(1 for r in rows if r.get("fallback")),
        "mem_hit_turns": mem_hit,
        "avg_ms": int(sum(ms) / len(ms)) if ms else 0,
        "last_err": "",
    }
    try:
        b = _LOCAL.get("brain")
        out["last_err"] = (getattr(b, "last_err", "") or "") if b else ""
        out["model"] = str((API_CFG or {}).get("model") or "")
    except Exception:
        pass
    try:
        import voice
        out["voice"] = voice.describe(API_CFG)
    except Exception:
        pass
    return out


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


def _cloud_diary_read(date_str):
    """读一篇日记的正文（剥掉存盘时的日期标题行），没有就返回空串。"""
    import re
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


def _cloud_history(limit=60):
    """最近 N 条聊天记录（t / role / text），给手机 App 显示会话用。"""
    p = data_file("chat_history.jsonl",
                  r"C:\linzhixia\data\chat_history.jsonl")
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


def start_remote_api(brain):
    """给"远程桌宠"和手机 App 开的门：客户端是薄壳，经隧道/直连用云上的大脑
    聊天 —— 记忆 / 人设 / 生活只有云上一份，天然同步。

    只绑 127.0.0.1 是默认值（公网碰不到）；要让手机直连得显式配
    bind_host: "0.0.0.0"，那时必须配 brain_token（没配就拒绝启动，见函数末尾）。
    同一时间只允许一个进程养她。
    """
    tok = BRAIN_TOKEN
    port = 8788

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _check(self):
            """鉴权（判定逻辑见模块级 _auth_ok：那边是纯函数，有测试盯着）。"""
            try:
                from urllib.parse import urlparse, parse_qs
                q = parse_qs(urlparse(self.path).query)
                qtok = (q.get("token") or [""])[0]
            except Exception:
                qtok = ""
            return _auth_ok(self.headers.get("Authorization") or "",
                            qtok, tok, self.client_address[0])

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
            note_client_ip(self.client_address[0])
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except Exception:
                n = 0
            # 带图的 /api/chat 走 base64，正常也就几 MB。早先 n 多大就读多大，
            # 一个超大 Content-Length 就能把 2C2G 打满。
            if n > MAX_BODY:
                self._json(413, {"err": "body too large"})
                return
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception:
                body = {}
            path = self.path.split("?")[0]
            try:
                if path == "/api/chat":
                    _in_text = str(body.get("text") or "")
                    _disp = _in_text          # 存档用原文，注入的系统提示不带
                    # 话里带单号就进监控（每 30 分钟自动查），现状也塞给模型，
                    # 她回话时能顺口说一句"刚发出去了"
                    try:
                        import features as _feat
                        if _feat.on("packages"):
                            import packages as _pkg
                            _tok = (API_CFG.get("uapi") or {}).get("token") or ""
                            _regs = [_pkg.register(no, _tok)
                                     for no in _pkg.extract_tracking(_in_text)]
                            _regs = [r for r in _regs if r]
                            if _regs:
                                _in_text += "\n（系统提示：检测到快递单号 " + "、".join(
                                    _pkg.status_line(r) for r in _regs) + \
                                    "。已加入监控，每30分钟自动查一次，有进展你会主动告诉他）"
                    except Exception:
                        pass
                    try:
                        _proactive_note_user()   # 他说话了：给自适应频率喂反馈
                    except Exception:
                        pass
                    _snap = {}
                    ans, mode, err = ask_with_retry(
                        _in_text, body.get("img") or None, display=_disp,
                        trace_out=_snap)
                    # 出错就留一行（欠费 402、模型名写错、地址变了都在这儿现形）
                    if err:
                        try:
                            errlog.warn("api/chat", f"{mode}：{err}")
                        except Exception:
                            pass
                    # 每轮一行 trace：记忆命中 / 块大小 / 延迟 / token。
                    # 字段取自 _snap（持锁时抄的），别在这里现读共享对象
                    try:
                        _ag = 0
                        try:
                            import agenda
                            _ag = 1 if agenda.block() else 0
                        except Exception:
                            pass
                        _trace_write({
                            "t": time.strftime("%Y-%m-%d %H:%M"),
                            "kind": "chat",
                            "in_len": len(_in_text),
                            "blocks": _snap.get("blocks") or {},
                            "mem_hits": _snap.get("mem_hits", 0),
                            "mem_top": _snap.get("mem_top") or [],
                            "agenda": _ag,
                            "model": _snap.get("model")
                            or str((API_CFG or {}).get("model") or ""),
                            "ms": _snap.get("ms"),
                            "tok_in": _snap.get("tok_in"),
                            "tok_out": _snap.get("tok_out"),
                            "tool": _snap.get("tool") or "",
                            "mode": mode,
                            "fallback": 1 if err or mode == "API失败" else 0,
                        })
                    except Exception:
                        pass
                    # reply 整段保留给旧版 App；messages 按她的换行拆好段，
                    # 新版 App 逐条渲染成连发气泡，存档也按这个粒度拆
                    self._json(200, {"reply": ans, "messages": split_messages(ans),
                                     "mode": mode, "err": err,
                                     "t": time.strftime("%Y-%m-%d %H:%M")})
                elif path == "/api/packages/del":
                    # 删掉就移出监控，她的提醒跟着停（poll 只扫这份存档）
                    import packages as _pkg
                    ok = _pkg.remove(str(body.get("number") or "").upper())
                    self._json(200, {"ok": bool(ok)})
                elif path == "/api/packages/note":
                    import packages as _pkg
                    p = _pkg.set_note(str(body.get("number") or "").upper(),
                                      str(body.get("note") or ""))
                    self._json(200, {"ok": bool(p), "rec": p})
                elif path == "/api/proactive":
                    try:
                        with _chat_lock():
                            ans, mode = brain.speak_on_scene(
                                str(body.get("scene") or ""))
                    except _Busy:
                        self._json(200, {"reply": "", "mode": "忙碌",
                                         "err": "她正忙着，等会儿再说"})
                        return
                    self._json(200, {"reply": ans, "mode": mode, "err": ""})
                elif path == "/api/reset":
                    try:
                        with _chat_lock():
                            if hasattr(brain, "reset"):
                                brain.reset()
                    except _Busy:
                        self._json(200, {"ok": False, "err": "她正忙着"})
                        return
                    self._json(200, {"ok": True})
                elif path == "/api/history/clear":
                    # 存档只有云上这一份。早先桌宠直接调本地 Brain.clear_history()
                    # 删的是本机文件，云上那份一动不动，看着清了其实没清。
                    ok = False
                    try:
                        from brain import Brain
                        ok = bool(Brain.clear_history())
                    except Exception as e:
                        print(f"[大脑] 清空聊天存档失败：{e}", flush=True)
                    try:
                        with _chat_lock():
                            if hasattr(brain, "reset"):
                                brain.reset()
                    except _Busy:
                        pass
                    self._json(200, {"ok": ok})
                elif path == "/api/life/catchup":
                    # 会调模型，慢，客户端放在后台线程里等
                    try:
                        life = getattr(brain, "life", None)
                        if life is None:
                            self._json(200, {"ok": False, "events": 0,
                                             "diary": 0, "err": "生活引擎没开"})
                            return
                        res = life.catch_up(force=bool(body.get("force"))) or {}
                        self._json(200, {"ok": True,
                                         "events": int(res.get("events") or 0),
                                         "diary": int(res.get("diary") or 0),
                                         "skip": res.get("skip") or ""})
                    except Exception as e:
                        self._json(200, {"ok": False, "events": 0, "diary": 0,
                                         "err": str(e)[:80]})
                elif path == "/api/diary/write":
                    try:
                        life = getattr(brain, "life", None)
                        ds = str(body.get("date") or "") \
                            or time.strftime("%Y-%m-%d")
                        txt = life.write_diary(ds) if life is not None else ""
                        self._json(200, {"ok": bool(txt), "date": ds,
                                         "body": txt or ""})
                    except Exception as e:
                        self._json(200, {"ok": False, "date": "",
                                         "body": "", "err": str(e)[:80]})
                elif path == "/api/reload_persona":
                    try:
                        with _chat_lock():
                            if hasattr(brain, "reload_persona"):
                                brain.reload_persona()
                    except _Busy:
                        self._json(200, {"ok": False, "err": "她正忙着"})
                        return
                    self._json(200, {"ok": True})
                elif path == "/api/persona/save":
                    # 保存人设字段。与切换人设不同：改的是当前这套的文件本身。
                    # 落盘 + 热重载两处都要，只改一处会出现"界面变了、她没变"
                    try:
                        import persona_store
                        key = os.path.basename(str(body.get("key") or "")
                                               or persona_store.key_from_config(api_config))
                        if not key or not os.path.isfile(persona_store.path_of(key)):
                            self._json(200, {"ok": False, "err": "没有这套人设"})
                            return
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            self._json(200, {"ok": False, "err": "字段不对"})
                            return
                        name = str(fields.get("name") or "").strip()
                        if not name:
                            self._json(200, {"ok": False, "err": "总得有个名字"})
                            return
                        persona_store.save(fields, key)
                        # 只有改的是当前那套才热重载：编辑别的模板不该动她
                        if key == persona_store.key_from_config(api_config):
                            with _chat_lock():
                                brain.reload_persona()
                        print("[大脑] 人设已更新：%s（%s）" % (name, key),
                              flush=True)
                        self._json(200, {"ok": True, "name": name, "key": key})
                    except Exception as e:
                        errlog.log_exc("api/persona/save", e)
                        self._json(200, {"ok": False, "err": str(e)[:120]})
                elif path == "/api/persona/apply":
                    # 改 config.json 的 persona_file 再热重载。key 只认 personas/
                    # 下已有的文件名（防路径穿越）
                    try:
                        import persona_store
                        key = os.path.basename(str(body.get("key") or ""))
                        if not key or not os.path.isfile(
                                persona_store.path_of(key)):
                            self._json(200, {"ok": False,
                                             "err": "没有这套人设"})
                            return
                        cfg = {}
                        try:
                            cfg = json.load(open(config_path(), encoding="utf-8"))
                        except Exception:
                            cfg = {}
                        cfg["persona_file"] = "personas/%s.json" % key
                        with open(config_path(), "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        # 内存那份也要改：reload_persona() 读的是内存的 persona_file，
                        # 只写盘的话会载回旧人设
                        api_config["persona_file"] = cfg["persona_file"]
                        with _chat_lock():
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
                elif path == "/api/persona/new":
                    # 新建一套人设模板（不自动切过去，省得她换脸换名字你还没看仔细）
                    try:
                        import persona_store
                        key = persona_store.create(
                            body.get("name"),
                            from_key=body.get("from") or None,
                            label=body.get("label") or "",
                            desc=body.get("desc") or "")
                        print("[大脑] 新建人设模板 %s" % key, flush=True)
                        self._json(200, {"ok": True, "key": key})
                    except ValueError as e:
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                    except Exception as e:
                        errlog.log_exc("api/persona/new", e)
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/persona/del":
                    # 删模板。不热重载：被删的不是当前那套（remove 已挡掉）
                    try:
                        import persona_store
                        ok, why = persona_store.remove(
                            body.get("key"),
                            current_key=persona_store.key_from_config(api_config))
                        if ok:
                            print("[大脑] 删除人设模板 %s"
                                  % str(body.get("key"))[:40], flush=True)
                        self._json(200, {"ok": ok, "err": why})
                    except Exception as e:
                        errlog.log_exc("api/persona/del", e)
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/voice/apply":
                    # 同人设切换，但内存里要同步两处：api_config 和 API_CFG
                    # （voice.synth() 实际收到的浅拷贝）。只改一处会"看着换了、声音没变"
                    try:
                        key = os.path.basename(str(body.get("key") or ""))
                        import voice
                        sub, desc = voice.apply(api_config, key)
                        if not sub:
                            self._json(200, {"ok": False,
                                             "err": "没有这个声音"})
                            return
                        cfg = {}
                        try:
                            cfg = json.load(open(config_path(), encoding="utf-8"))
                        except Exception:
                            cfg = {}
                        cfg["voice"] = sub
                        # 老配置备份一次就够，别每次点都盖
                        bak = config_path() + ".bak_voice"
                        if not os.path.exists(bak):
                            try:
                                import shutil
                                shutil.copyfile(config_path(), bak)
                            except Exception:
                                pass
                        with open(config_path(), "w", encoding="utf-8") as f:
                            json.dump(cfg, f, ensure_ascii=False, indent=2)
                        api_config["voice"] = sub
                        API_CFG["voice"] = sub
                        # 不用重启也不用重建 Brain，synth() 每次现读配置。
                        # 缓存按 (文本, 模型, 音色) 做 key，换完不串音
                        print(f"[大脑] 声音已切换为 {desc}（API_CFG 已同步）",
                              flush=True)
                        self._json(200, {"ok": True, "key": key, "desc": desc,
                                         "voice": voice.describe(API_CFG)})
                    except Exception as e:
                        errlog.log_exc("api/voice/apply", e)
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/voice/preview":
                    # 用一份临时配置去合成，别动全局 api_config / API_CFG，
                    # 否则"试听"就变成"直接换掉她的声音"
                    try:
                        key = os.path.basename(str(body.get("key") or ""))
                        import voice
                        sub, desc = voice.apply(api_config, key)
                        if not sub:
                            self._json(200, {"ok": False,
                                             "err": "没有这个声音"})
                            return
                        tmp = dict(API_CFG)
                        tmp["voice"] = sub
                        text = (str(body.get("text") or "").strip()
                                or voice.PREVIEW_TEXT)
                        name = voice.synth(text, tmp)
                        if not name:
                            self._json(200, {"ok": False,
                                             "err": "合成失败（余额或网络）"})
                            return
                        self._json(200, {"ok": True, "name": name,
                                         "text": text, "desc": desc})
                    except Exception as e:
                        errlog.log_exc("api/voice/preview", e)
                        self._json(200, {"ok": False, "err": str(e)[:80]})
                elif path == "/api/models/apply":
                    # 写盘之后要 clear + update 内存那份（Brain 拿的是启动时
                    # load_config() 的字典），不然换了模型她还用旧的
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
                                "current": model_hub.public_current(api_config)})
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
                elif path == "/api/features":
                    # App 设置页的功能开关：传哪个改哪个，返回改后的全量
                    import features as _feat
                    patch = body if isinstance(body, dict) else {}
                    patch.pop("token", None)
                    cur = _feat.set_features(patch) if patch else None
                    if cur is None:
                        cur = _feat.all_features()
                    self._json(200, {"features": cur})
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
            note_client_ip(self.client_address[0])
            path = self.path.split("?")[0]
            if path == "/api/persona/full":
                # 给人设编辑器用：读人设的全部字段（不是摘要）。
                # 放GET 分支：编辑器只是读，POST 那条路留给 save。
                try:
                    import persona_store
                    from urllib.parse import urlparse, parse_qs
                    _q = parse_qs(urlparse(self.path).query)
                    key = (_q.get("key") or [None])[0]
                    # key 来自查询串，走 basename + isfile双重校验，防路径穿越
                    cur = os.path.basename(str(key)) if key else \
                        persona_store.key_from_config(api_config)
                    if not cur or not os.path.isfile(persona_store.path_of(cur)):
                        self._json(200, {"ok": False, "err": "没有这套人设"})
                        return
                    _p = persona_store.load(cur)
                    self._json(200, {"ok": True, "key": cur,
                                     "fields": {k: _p.get(k) for k in
                                                persona_store.FIELDS}})
                except Exception as e:
                    self._json(200, {"ok": False, "err": str(e)[:80]})
            elif path == "/api/persona":
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
                self._json(200, {"name": name, "key": cur, "presets": presets,
                                 "relation": persona_store.relation_of(
                                     persona_store.load(cur)),
                                 "templates": persona_store.list_templates(cur),
                                 "max": persona_store.MAX_TEMPLATES,
                                 "used": persona_store.template_count()})
            elif path == "/api/voices":
                # 清单是 VOICE_CATALOG 里的死数据，不联网 —— 设置页必须永远打得开
                try:
                    import voice
                    # 顺带扫一眼缓存目录，让 App 看到"这个声音攒了几条"，
                    # 只用来判断要不要清缓存
                    try:
                        n_cache = len([f for f in os.listdir(voice.VOICE_DIR)
                                       if f.startswith("v_")])
                    except Exception:
                        n_cache = 0
                    d = voice.catalog(API_CFG)
                    d["ok"] = True
                    d["cache_files"] = n_cache
                    d["current_desc"] = voice.describe(API_CFG)
                    self._json(200, d)
                except Exception as e:
                    errlog.log_exc("api/voices", e)
                    self._json(200, {"ok": False, "err": str(e)[:120],
                                     "current": "", "items": []})
            elif path == "/api/models":
                # force=1 = 用户点了"重新拉取"，不用缓存（换 key 之后必须这样拉一次）
                try:
                    force = str(_query_param(self.path, "force") or "") == "1"
                    cur = model_hub.current(api_config)
                    lst = model_hub.list_models(cur["api_base"],
                                                cur["api_key"], force=force)
                    # 回客户端用脱敏版：cur 里的真 key 只留在这一次调用里
                    self._json(200, {
                        "ok": True, "current": model_hub.public_current(api_config),
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
                        "current": model_hub.public_current(api_config),
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
            elif path == "/api/stats":
                # 用量与状态：App 设置页拿它显示"今天聊了多少轮、花了多少 token"
                out = _stats_today()
                try:
                    import agenda
                    out["agenda"] = agenda.stats()
                except Exception:
                    pass
                self._json(200, out)
            elif path == "/api/trace":
                n = _query_param(self.path, "last") or 20
                self._json(200, {"items": _trace_tail(n)})
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
            elif path == "/api/features":
                # App 设置页读功能开关
                import features as _feat
                self._json(200, {"features": _feat.all_features()})
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
            elif path == "/api/packages":
                # App 快递管理页：监控中的单号列表
                try:
                    import packages
                    self._json(200, {"items": packages.list_all()})
                except Exception as e:
                    self._json(200, {"items": [], "err": str(e)[:80]})
            elif path == "/api/stickers":
                # 只回表情包库（data/stickers）；upload 里是聊天/自拍图，倒进来
                # App 面板会塞满他没添加过的图
                names = []
                try:
                    import stickers
                    names = list(stickers.all_names())
                except Exception:
                    names = []
                self._json(200, {"names": names})
            elif path == "/api/voice":
                # 她发的语音条（voice.py 合成，文件在 data/voice/）
                name = _query_param(self.path, "name")
                try:
                    import voice
                    p = voice.path_of(name)
                except Exception:
                    p = None
                if not p:
                    self._json(404, {"err": "no such voice"})
                    return
                try:
                    with open(p, "rb") as f:
                        blob = f.read()
                except OSError:
                    self._json(404, {"err": "read fail"})
                    return
                # 后缀 → MIME。阿里那条线的输出格式可配（wav / opus 都合法），
                # 写死 audio/mpeg 会让 wav 播不出来
                _ct = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                       ".opus": "audio/ogg", ".ogg": "audio/ogg"}
                self.send_response(200)
                self.send_header("Content-Type",
                                 _ct.get(os.path.splitext(p)[1].lower(),
                                         "application/octet-stream"))
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
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
    # `bind_host: "0.0.0.0"`，靠 token 兜底鉴权；旧配置里这个值在
    # mobile.remote_host，继续认
    host = str(api_config.get("bind_host")
               or MOB_CFG.get("remote_host") or "127.0.0.1")
    # 没配 token 就别往外绑：那等于开了一个谁都能用的接口（以前 _check 是空 token
    # 全放行，忘填 token 很常见，配上 0.0.0.0 就是公网裸奔）。宁可不开这个口。
    if not tok and not _is_loopback(host):
        print("[大脑] 拒绝启动远程 API：bind_host=%s 但没配 brain_token。"
              "这样会变成无鉴权的公开接口 —— 请在 config.json 里填一个"
              "随机长串（brain_token），或改回只绑 127.0.0.1。" % host,
              flush=True)
        return None
    if not tok:
        print("[大脑] 远程 API 未配 brain_token：只接受本机回环连接"
              "（开发模式，手机/公网连不进来）", flush=True)
    srv = http.server.ThreadingHTTPServer((host, port), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[大脑] 远程大脑 API 就绪：{host}:{port}", flush=True)
    return srv


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
        # 她的全部"生活"：生活补算 / 历史摘要 / 朋友圈 / 主动搭话 / 事件提醒
        start_background(_LOCAL["brain"])

    print("=" * 58, flush=True)
    print(" 云端大脑", flush=True)
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
