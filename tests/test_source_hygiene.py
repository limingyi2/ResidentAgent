# -*- coding: utf-8 -*-
"""源码级的卫生检查（纯静态，不 import 被测模块）。

这里盯的是几类**不会报错、只会静默出错**的写法：

1. **同一个作用域里重复定义同名函数/方法** —— Python 里后者覆盖前者，没有警告。
   这个仓库真踩过两次：`chat.html` 里 `copyText` 定义了两遍（前一份成了死代码），
   `remote_brain.py` 里 `RemoteBrain._post` 定义了两遍（旧的那份把 token 拼在 URL 上，
   新那份走 Authorization 头 —— 靠"后定义的赢"才碰巧是对的，读代码的人完全看不出）。
2. **空的测试文件** —— 一个 `test_*.py` 里没有 TestCase，收集到 0 个用例却"通过"。
"""
import ast
import collections
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# plugins/ 也要扫：插件是新增功能最容易扎堆的地方，一份文件里写两遍
# 同一个 helper 就是"后者悄悄赢"，而插件坏了只打一行日志，很难发现
SCAN_DIRS = ("brain", "pet", "tests")


def _defs(body):
    """这个作用域里定义了的函数名。

    带 @x.setter / @x.deleter 的**要跳过**：属性写法里 getter 和 setter
    合法地同名（`def hist` + `@hist.setter def hist`），在 AST 里都是
    FunctionDef，但不构成"后者覆盖前者"。不排除的话每个 property 都会被
    误报成重复定义（Brain.hist 就有一个，渠道隔离时加的）。
    """
    return [n.name for n in body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not _is_accessor(n)]


def _is_accessor(node):
    """是不是属性的 setter/deleter。

    写法是 `@hist.setter`（不是 `@foo(hist.setter)`），所以装饰器本身就是
    一个 Attribute 节点，attr 为 setter/deleter。
    """
    for d in getattr(node, "decorator_list", []) or []:
        if isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter"):
            return True
    return False


def _files():
    """要扫的 .py 列表，去重且有序。

    brain 用非递归 glob：递归会把 plugins/ 也带上，而 plugins 又在 SCAN_DIRS
    里显式列着，同一个文件就会报两遍错，测试输出成了噪音。
    """
    out = []
    seen = set()
    for d, pat in (("brain", "brain/*.py"),
                   ("brain/plugins", "brain/plugins/*.py"),
                   ("pet", "pet/*.py"),
                   ("tests", "tests/*.py")):
        for p in sorted(glob.glob(os.path.join(ROOT, pat))):
            rp = os.path.relpath(p, ROOT)
            if rp not in seen:
                seen.add(rp)
                out.append(p)
    return out


def _tree(path):
    """读文件并解析成 AST。用 with 关句柄 —— 裸 open().read() 会漏 ResourceWarning。"""
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=path)


class TestNoDuplicateDefinitions(unittest.TestCase):

    def _dup_in(self, names):
        return sorted(k for k, v in collections.Counter(names).items() if v > 1)

    def test_no_module_defines_a_name_twice(self):
        bad = []
        for p in _files():
            tree = _tree(p)
            dup = self._dup_in(_defs(tree.body))
            if dup:
                bad.append("%s: %s" % (os.path.relpath(p, ROOT), dup))
        self.assertEqual(bad, [], "同名的顶层函数被定义了两次（后者静默覆盖前者）")

    def test_no_class_defines_a_method_twice(self):
        bad = []
        for p in _files():
            tree = _tree(p)
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                dup = self._dup_in(_defs(node.body))
                if dup:
                    bad.append("%s: %s.%s -> %s"
                               % (os.path.relpath(p, ROOT),
                                  node.name, dup, dup))
        self.assertEqual(bad, [], "同一个类里重复定义了方法（后者静默覆盖前者）")

    def test_nested_functions_are_not_duplicated_either(self):
        """闭包里的同名函数也一样：后定义的赢。"""
        bad = []
        for p in _files():
            tree = _tree(p)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    dup = self._dup_in(_defs(node.body))
                    if dup:
                        bad.append("%s: %s() 里 -> %s"
                                   % (os.path.relpath(p, ROOT),
                                      node.name, dup))
        self.assertEqual(bad, [])


class TestTestFilesAreNotEmpty(unittest.TestCase):

    def test_every_test_file_has_at_least_one_testcase(self):
        """空的 test_*.py 会被 unittest 当成"通过"，看着有覆盖其实没有。"""
        tdir = os.path.join(ROOT, "tests")
        offenders = []
        for p in sorted(glob.glob(os.path.join(tdir, "test_*.py"))):
            tree = _tree(p)
            has_case = any(
                isinstance(n, ast.ClassDef)
                and any(isinstance(b, ast.Attribute) and b.attr == "TestCase"
                        for b in n.bases)
                for n in tree.body)
            if not has_case:
                offenders.append(os.path.basename(p))
        self.assertEqual(offenders, [], "这些测试文件里一个 TestCase 都没有")

    def test_no_test_class_has_zero_test_methods(self):
        """只有 setUp/helper、没有 test_* 的 TestCase 等于没写。"""
        bad = []
        for p in sorted(glob.glob(os.path.join(ROOT, "tests", "test_*.py"))):
            tree = _tree(p)
            for n in tree.body:
                if not isinstance(n, ast.ClassDef):
                    continue
                if not any(isinstance(b, ast.Attribute) and b.attr == "TestCase"
                           for b in n.bases):
                    continue
                tests = [m for m in _defs(n.body) if m.startswith("test")]
                if not tests:
                    bad.append("%s.%s" % (os.path.basename(p), n.name))
        self.assertEqual(bad, [], "这些 TestCase 里没有一个 test_* 方法")


if __name__ == "__main__":
    unittest.main()
