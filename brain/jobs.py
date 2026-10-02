# -*- coding: utf-8 -*-
"""后台循环：她的"生活"全在这几根线程里跑。

- 生活补算（半小时）      过日子 + 夜里写日记
- 历史摘要（20 分钟）    滑出窗口的旧对话压成摘要，让她不忘前几天；顺路刷约定账本
- 朋友圈（每小时）       让她决定发不发；顺路刷天气/热搜缓存
- 主动搭话（自适应间隔）  静默时段/每日上限/被回应率全算进去
- 事件提醒（30 分钟）    快递进展 + 天气突变，走主动搭话同一条路弹通知

所有循环的共同纪律：出错打印一行继续跑，别让任何一根线程把进程带崩
"""
import time
import threading

from runtime import _LOCAL, API_CFG, _trace_write
from reply import strip_say_marker, resolve_gen_tags, resolve_voice_tag
from proactive import (
    PROACTIVE_RULES, _proactive_conf, _in_quiet,
    _proactive_interval_multiplier, _proactive_stats_load,
    _proactive_used_today, _proactive_count_up, _proactive_note_sent,
    _is_repeat_of_recent, _cloud_append_assistant, _his_ip)
from moment_flow import try_post_moment, _SEEDED_AT, _seed_initial_moment


def start_background(brain):
    """把她的"生活"全部跑起来（run_server 装配完大脑后调一次）。"""
    threading.Thread(target=_life_loop, args=(brain,), daemon=True).start()
    threading.Thread(target=_recap_loop, args=(brain,), daemon=True).start()
    threading.Thread(target=_moment_loop, args=(brain,), daemon=True).start()
    threading.Thread(target=_seed_initial_moment, args=(brain,), daemon=True).start()
    threading.Thread(target=_proactive_loop, args=(brain,), daemon=True).start()
    threading.Thread(target=_reminder_loop, args=(brain,), daemon=True).start()


def _trace_loop(kind, **kw):
    """自治循环也落一行 trace。

    以前 trace 只有 /api/chat 一条路有，"她为什么主动说了这句""这轮为什么没发"
    只能靠翻 stdout（重启就没了）。去重与静默闸门拦掉的每一轮同样记下来 ——
    拦了多少次是判断"频率合不合适"的唯一依据。
    """
    try:
        time_str = time.strftime("%Y-%m-%d %H:%M")
        rec = {"t": time_str, "kind": kind}
        rec.update(kw)
        _trace_write(rec)
    except Exception:
        pass


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


def _life_loop(b):
    """生活补算：她过日子 + 夜里写日记（云上没有桌宠定时器，靠这个补）。
    catch_up 幂等 + 自带节流，循环着跑才不会漏掉跨天。"""
    while True:
        catch_up_life_local(b)
        time.sleep(1800)


def _recap_loop(b):
    """历史摘要：把滑出上下文窗口的旧对话压成"聊过什么"再存，否则旧话题转头就忘。启动 60 秒先补积压，之后每 20 分钟一轮。"""
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


def _refresh_world_caches():
    """顺路刷天气 + 热搜缓存（失败静默，聊天链路只读缓存文件）。

    天气城市跟他的 IP 走；热搜进 6 小时槽位才刷（0/6/12/18 点整）
    """
    try:
        tok = (API_CFG.get("uapi") or {}).get("token")
        fallback = (_LOCAL["brain"].life.world.get("city")
                    if getattr(_LOCAL.get("brain"), "life", None) else "")
        if tok:
            import features as _feat
            import weather_cache, hotboard_cache
            if _feat.on("weather"):
                weather_cache.refresh_auto(_his_ip(), tok, fallback)
            if _feat.on("hotboard") and hotboard_cache.should_refresh():
                plat = ((API_CFG.get("uapi") or {})
                        .get("hotboard_type") or "douyin")
                hotboard_cache.refresh(plat, tok)
    except Exception:
        pass


def _moment_loop(b):
    """朋友圈：每小时让她"想想要不要发"，发不发、发什么由她定。"""
    import moments
    first = True
    while True:
        try:
            # 启动后 150s 先快跑一次，保证"今天有东西可看"。刚种过开场圈就
            # 跳过这次，免得两分钟内连发两条，之后恢复每小时一次
            time.sleep(150 if first else 3600)
            if first and (time.time() - _SEEDED_AT["t"]) < 1500:
                first = False
                continue
            first = False
            _refresh_world_caches()
            if moments.today_count() >= 4:
                continue        # 一天最多四条（自拍/吃的/风景/猫狗/八卦都能发，活跃一天很正常）
            raw, posted = try_post_moment(b)
            if posted:
                _trace_loop("moment", posted=1, chars=len(raw or ""))
                print("[大脑] 她发了一条朋友圈", flush=True)
            else:
                # 必须把原话打出来：她可能只是"这轮不想发"，也可能是格式跑偏
                _trace_loop("moment", posted=0, raw=(raw or "")[:80])
                print("[大脑] 朋友圈：这轮没发，她的原话＝%s"
                      % raw.replace("\n", " / ")[:140], flush=True)
        except Exception as e:
            print(f"[大脑] 朋友圈循环出错：{str(e)[:80]}", flush=True)


def _proactive_loop(b):
    """主动搭话：同样由她决定说不说；说了就写进聊天存档，
    手机 App 的后台轮询会拉到这条消息并弹通知。"""
    while True:
        try:
            # 每轮重新读配置：改完 config.json 不用重启大脑就生效
            pa = _proactive_conf()
            base = int(pa.get("interval_sec") or 2400)
            interval = base
            if pa.get("adaptive", True):
                # 伸缩上限 2 小时。再冷也别变成半小时一条，那已经算骚扰
                interval = int(base * _proactive_interval_multiplier())
                cap = int(pa.get("max_interval_sec") or 7200)
                interval = min(interval, cap)
                # 他刚说过话（15 分钟内）→ 人在，不需要"找"他
                _d = _proactive_stats_load()
                _gap = time.time() - int(_d.get("last_user_ts") or 0)
                if _gap < 900:
                    time.sleep(max(300, interval))
                    continue
            time.sleep(max(300, interval))
            if not pa.get("enabled", True):
                continue
            # 静默时段默认 23:00~07:00，必须限制，否则凌晨也会连发搭话
            qs = pa.get("quiet_start", "23:00")
            qe = pa.get("quiet_end", "07:00")
            if _in_quiet(qs, qe):
                print("[大脑] 静默时段（%s~%s），这轮不主动搭话"
                      % (qs, qe), flush=True)
                continue
            # 每日上限：一天几十条比不说话更烦人
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
                _trace_loop("proactive", sent=0, reason="她自己不想说")
                continue
            text = resolve_gen_tags(text)
            if not text:
                _trace_loop("proactive", sent=0, reason="标签解析后为空")
                continue
            text = resolve_voice_tag(text, API_CFG)
            # 去重闸门：最近说过（或截一段说过）就不发第二遍。主动搭话会
            # 复述上下文里自己刚说的话，见 proactive._recent_her_texts
            if _is_repeat_of_recent(text):
                print("[大脑] 主动搭话：和最近说过的话重复，这轮不发",
                      flush=True)
                _trace_loop("proactive", sent=0, reason="和最近说过的话重复")
                continue
            _cloud_append_assistant(text)
            _proactive_count_up()
            _proactive_note_sent()      # 记下这次主动，等他反应（自适应频率用）
            _trace_loop("proactive", sent=1, chars=len(text),
                        n_today=used + 1, interval_sec=interval)
            print("[大脑] 她主动找他说话了（今天第 %d 次）"
                  % (used + 1), flush=True)
        except Exception as e:
            print(f"[大脑] 主动搭话循环出错：{str(e)[:80]}", flush=True)


def _reminder_loop(b):
    """事件提醒循环：快递每 30 分钟查一次（进监控的单号）、天气变化按规则触发。
    生成走 chat(proactive=True)，发出去走主动搭话同一条路（App 轮询拉到弹通知）。
    静默时段照躲：快递到了也不凌晨轰炸，睡醒那轮补说。"""

    def _say(prompt):
        with _LOCAL["lock"]:
            ans, _m = b.chat(prompt, proactive=True, log=False)
        text = strip_say_marker((ans or "").strip())
        if not text or text == "无":
            _trace_loop("reminder", sent=0, reason="她自己不想说")
            return
        _cloud_append_assistant(text)
        _trace_loop("reminder", sent=1, chars=len(text))
        print("[大脑] 事件提醒：%s" % text[:50], flush=True)

    time.sleep(90)           # 启动先让缓存和大脑站稳
    while True:
        try:
            pa = _proactive_conf()
            if _in_quiet(pa.get("quiet_start", "23:00"),
                         pa.get("quiet_end", "07:00")):
                time.sleep(1800)
                continue
            tok = (API_CFG.get("uapi") or {}).get("token") or ""
            import features as _feat
            # 天气：代码判档位（雨/雪/高温/低温/大风，一天一档只报一次）
            try:
                if _feat.on("weather_alert"):
                    import weather_cache
                    trg = weather_cache.check_trigger()
                    if trg:
                        event, detail = trg
                        city = weather_cache.city() or "他那边"
                        _say("[系统指令] 他那边现在天气有变化，你要主动发一条"
                             "消息关心他。\n[触发事件] 他那边（%s）%s（%s）。\n"
                             "[回复规则] 用你平时的口气关心一句（提醒带伞/"
                             "加衣服/别中暑都行），顺带说一句你此刻正在做的"
                             "小事，50字以内。别写成天气预报，别问他住哪。"
                             % (city, event, detail))
            except Exception as e:
                print("[大脑] 天气提醒出错：%s" % str(e)[:60], flush=True)
            # 快递：状态变到 派送中/签收/异常 才说
            try:
                if _feat.on("packages"):
                    import packages
                    for ev in packages.poll(tok):
                        rec = ev["rec"]
                        _say("[系统指令] 他的快递有新进展，你要主动告诉他。\n"
                             "[快递] %s\n[回复规则] 用你平时的口气说，一两句，"
                             "别报物流流水账。派送中就提醒他去取，签收了就说"
                             "到了，异常就让他找卖家。"
                             % packages.status_line(rec))
            except Exception as e:
                print("[大脑] 快递提醒出错：%s" % str(e)[:60], flush=True)
            time.sleep(30 * 60)
        except Exception as e:
            print(f"[大脑] 提醒循环出错：{str(e)[:80]}", flush=True)
            time.sleep(600)
