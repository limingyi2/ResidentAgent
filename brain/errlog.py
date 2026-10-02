# -*- coding: utf-8 -*-
"""错误日志：把"到底哪一步炸了"记下来，App 能直接导出去看。

之前出错只能靠云上的控制台窗口看，手机上一片空白 —— 只知道"她不回话"，不知道是
欠费（402）、地址变了、还是模型名写错。这个模块把每次异常写成一行 JSON，App 设置页
能拉下来看、能导出成 txt。

两份日志：server 是云上她自己出的错，client 是手机上 App 出的错（由 App 上报）。
每行一条：{"t":…, "level":…, "where":…, "type":…, "msg":…, "trace":…}
文件到 2MB 就砍掉前面一半 —— 日志是给人看的，不用留一辈子。
"""
import os
import json
import time
import threading
import traceback

try:
    from paths import LOGS_DIR
except Exception:                      # 单独跑这个文件时的兜底
    LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()

_FILES = {
    "server": "error_server.jsonl",
    "client": "error_client.jsonl",
}


def path_of(src="server"):
    d = LOGS_DIR
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return os.path.join(d, _FILES.get(src, _FILES["server"]))


def _trim(path):
    """太大就砍掉前面一半（按行砍，别砍出半行 JSON）。"""
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[len(lines) // 2:])
    except Exception:
        pass


def log(where, msg, level="ERROR", src="server", trace=""):
    """记一条。msg 可以是字符串，也可以是异常对象（自动拆类型和堆栈）。"""
    etype, emsg = "", str(msg or "")
    if isinstance(msg, BaseException):
        etype = type(msg).__name__
        emsg = str(msg)
        if not trace:
            trace = "".join(traceback.format_exception(
                type(msg), msg, msg.__traceback__))[-1500:]
    rec = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "where": str(where or "")[:60],
        "type": etype,
        "msg": emsg[:500],
        "trace": trace[:1500],
    }
    try:
        with _LOCK:
            p = path_of(src)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            _trim(p)
    except Exception:
        pass                            # 记日志这件事本身不该把程序搞崩
    return rec


def log_exc(where, e, src="server"):
    return log(where, e, level="ERROR", src=src)


def warn(where, msg, src="server"):
    return log(where, msg, level="WARN", src=src)


def recent(n=200, src="server"):
    """最近 n 条，新的在前（App 直接渲染）。"""
    n = max(1, min(int(n or 200), 2000))
    p = path_of(src)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
    except Exception:
        return []
    out = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        try:
            out.append(json.loads(l))
        except Exception:
            out.append({"t": "", "level": "ERROR", "where": "-",
                        "type": "", "msg": l[:300], "trace": ""})
    out.reverse()
    return out


def as_text(n=200, src="server"):
    """导出用的纯文本（App 存成 .txt 发给人看）。"""
    items = recent(n, src)
    if not items:
        return "（没有错误记录）\n"
    head = ("# 错误日志（%s）\n# 导出时间：%s\n# 共 %d 条\n\n"
            % ("云上" if src == "server" else "手机",
               time.strftime("%Y-%m-%d %H:%M:%S"), len(items)))
    buf = [head]
    for it in items:
        buf.append("[%s] %s | %s | %s%s\n"
                   % (it.get("t") or "-", it.get("level") or "",
                      it.get("where") or "-",
                      (it.get("type") + "：" if it.get("type") else ""),
                      it.get("msg") or ""))
        if it.get("trace"):
            buf.append("    " + (it.get("trace") or "").replace("\n", "\n    ")
                       + "\n")
        buf.append("\n")
    return "".join(buf)


def clear(src="server"):
    try:
        p = path_of(src)
        if os.path.exists(p):
            os.remove(p)
        return True
    except Exception:
        return False


def stats():
    """给设置页显示"云上 X 条 / 手机 Y 条"。"""
    out = {}
    for k in _FILES:
        p = path_of(k)
        try:
            out[k] = sum(1 for _ in open(p, encoding="utf-8", errors="replace"))
        except Exception:
            out[k] = 0
    return out


def install_excepthook():
    """把没被 try 住的异常也记下来。

    两条路必须都装，少一条就漏一半：
    - sys.excepthook        → 主线程的未捕获异常
    - threading.excepthook  → 子线程的未捕获异常（Python 3.8+）

    只装前者的话，服务里那 6 条后台 daemon 线程静默死掉时一个字都不会留下 ——
    而它们恰恰是最容易"死了没人发现"的地方（主动搭话、摘要、朋友圈、生活循环）。
    """
    import sys
    import threading

    def hook(etype, e, tb):
        try:
            log("main", e, trace="".join(
                traceback.format_exception(etype, e, tb))[-1500:])
        except Exception:
            pass
        sys.__excepthook__(etype, e, tb)

    def thread_hook(args):
        try:
            log("thread", args.exc_value, trace="".join(
                traceback.format_exception(args.exc_type, args.exc_value,
                                           args.exc_traceback))[-1500:])
        except Exception:
            pass
        # 交回默认处理（只打印、不终止进程），行为和没装钩子时一致
        try:
            threading.__excepthook__(args)
        except Exception:
            traceback.print_exception(args.exc_type, args.exc_value,
                                      args.exc_traceback)

    sys.excepthook = hook
    if hasattr(threading, "excepthook"):        # 3.8+
        threading.excepthook = thread_hook


if __name__ == "__main__":
    log("self_test", "这是一条测试记录")
    print(as_text(5))
    print(stats())
