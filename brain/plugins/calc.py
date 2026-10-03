# -*- coding: utf-8 -*-
"""算数：她会自己算，但模型心算不可靠 —— 3 位数乘法就开始错。

这个插件让她能调一个真算器。**不联网、不调模型、纯 Python**，所以
零延迟零成本，也不会在网络抖动时把她的话卡住。
"""
import re

NAME = "calc"
DESC = "算数：加减乘除、百分比、幂，她自己心算容易错"


def _t_calc(arg):
    """算一个算式。参数就是算式本身：'128*3'、'(12+8)/2'、'15%'。"""
    s = str(arg or "").strip()
    if not s:
        return "算不了：没给算式"
    # 只留数字和四则运算括号小数点。表达式求值必须先过这道白名单 ——
    # eval 能干的事远不止算数，不能把原始字符串直接丢进去。
    expr = re.sub(r"[\s]", "", s)
    expr = expr.replace("×", "*").replace("÷", "/").replace("^", "**")
    expr = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)%", r"(\1/100)", expr)
    if not re.fullmatch(r"[0-9+\-*/().]+", expr):
        return "算不了：只支持加减乘除和括号"
    if len(expr) > 200:
        return "算不了：算式太长了"
    try:
        v = eval(expr, {"__builtins__": {}}, {})   # noqa: S307 白名单已过
    except ZeroDivisionError:
        return "算不了：除以 0 了"
    except Exception:
        return "算不了：算式有问题"
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return "算不了：结果不是一个正常数"
        v = round(v, 6)
        if v == int(v) and abs(v) < 1e15:
            v = int(v)
    return "%s = %s" % (s, v)


TOOLS = {"calc": _t_calc}

TOOL_HELP = (
    "- 算式：[tool:calc:算式] 算一下，参数直接写算式（128*3、"
    "(12+8)/2、15% 这种都行）。\n"
    "  涉及具体数字的计算一律走它，别自己心算 —— 你心算三位数乘法就会错，"
    "他拿来对账的数错了比不算更糟。\n"
)
