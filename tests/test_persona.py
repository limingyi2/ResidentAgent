# -*- coding: utf-8 -*-
"""人设装载：配置指向的文件必须存在，且缺失时**不能静默**。

背景（这是一次真实事故的回归）：仓库脱敏时把人设文件从
`personas/<角色名>.json` 改成了 `personas/default.json`，而本地 `config.json`
的 `persona_file` 还指着旧名字。`persona_store.load()` 当时是**完全静默**的
（读不到文件就返回内置值），于是她的自定义人设被悄悄换成空模板 ——
因为没有报错，这事可以一直不被发现。

仓库现在只发空模板（不预置任何角色），所以"配置指向的文件存在"是硬约束。
"""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brain"))

import persona_store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestShippedTemplateIsConsistent(unittest.TestCase):
    """发出去的那份模板必须自洽 —— 照它建 config.json 的人不该拿到一个没名字的她。"""

    def test_example_config_persona_file_exists(self):
        p = os.path.join(ROOT, "brain", "config", "config.example.json")
        with open(p, encoding="utf-8") as f:
            ex = json.load(f)
        key = persona_store.key_from_config(ex)
        self.assertTrue(
            os.path.isfile(persona_store.path_of(key)),
            "config.example.json 的 persona_file=%r 指向的文件不存在"
            "（改名之后忘了同步这里，就是那次事故的成因）"
            % ex.get("persona_file"))

    def test_default_key_has_a_shipped_file(self):
        p = persona_store.path_of(persona_store.DEFAULT_KEY)
        self.assertTrue(
            os.path.isfile(p),
            "DEFAULT_KEY=%r 对应的 %s 不存在：配置里不填 persona_file 时会静默用空模板"
            % (persona_store.DEFAULT_KEY, os.path.basename(p)))

    def test_every_shipped_persona_parses_and_uses_known_fields(self):
        """目录里每个 .json 都要能解析，字段只能落在白名单里（防手写打错）。"""
        files = [n for n in os.listdir(persona_store.PERSONA_DIR)
                 if n.endswith(".json")]
        self.assertTrue(files, "personas/ 下一个 .json 都没有")
        for n in files:
            with open(os.path.join(persona_store.PERSONA_DIR, n),
                      encoding="utf-8") as f:
                raw = json.load(f)                     # 解析不了就红
            self.assertIsInstance(raw, dict, n)
            unknown = [k for k in raw
                       if k not in persona_store.FIELDS and not k.startswith("_")]
            self.assertEqual(unknown, [],
                             "%s 里有不会被读取的字段（白名单外的会被 save() 丢掉）"
                             % n)

    def test_template_loads_into_the_full_field_set(self):
        """空模板也得返回完整字段集，缺 key 会让下游 KeyError/静默变空。"""
        data = persona_store.load(persona_store.DEFAULT_KEY)
        for k in persona_store.FIELDS:
            self.assertIn(k, data, "读出来的人设缺字段 %s" % k)

    def test_label_and_desc_stay_out_of_the_prompt(self):
        """label/desc 是给 App 列表用的，不该被拼进 system 提示词。

        白名单里加了这两个（不加点一下"保存"就丢，App 预设列表那行描述会变空），
        但 build_system_text 只该拼 name/背景/场景/性格/称呼/notes ——
        把列表描述塞进提示词等于让模型每轮都读一句不相干的广告。
        """
        data = dict(persona_store.DEFAULT_PERSONA)
        data.update({"name": "某人", "label": "唯一标记XYZ",
                     "desc": "唯一描述XYZ"})
        txt = persona_store.build_system_text(data)
        self.assertNotIn("唯一标记XYZ", txt)
        self.assertNotIn("唯一描述XYZ", txt)

    def test_label_and_desc_survive_a_save_roundtrip(self):
        """在 App 里编辑人设保存一次，列表显示名不能被静默丢掉。

        这就是补白名单的原因：FIELDS 是 save() 的唯一依据，不在里面 = 存完消失。
        """
        import tempfile
        real_dir = persona_store.PERSONA_DIR
        with tempfile.TemporaryDirectory() as td:
            persona_store.PERSONA_DIR = td
            try:
                persona_store.save({"name": "某人", "label": "原版 · 老朋友",
                                    "desc": "她自己过日子"}, "probe")
                with open(persona_store.path_of("probe"),
                          encoding="utf-8") as f:
                    back = json.load(f)
            finally:
                persona_store.PERSONA_DIR = real_dir
        self.assertEqual(back.get("label"), "原版 · 老朋友")
        self.assertEqual(back.get("desc"), "她自己过日子")


class TestMissingPersonaIsLoud(unittest.TestCase):
    """缺失必须出声 —— 静默就是那次事故能潜伏的原因。"""

    def _capture(self, key):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            data = persona_store.load(key)
        return data, buf.getvalue()

    def test_missing_file_is_reported_not_silent(self):
        """配置那份不在、而默认那份在 → 明确说出改用了哪一份。

        这是最常见的形态（改名后 config 还指着旧名），也是那次事故的现场。
        """
        data, out = self._capture("definitely-not-a-real-persona-key")
        self.assertIn("不存在", out, "读不到人设文件竟然一句话都不说")
        self.assertIn(os.path.basename(
            persona_store.path_of(persona_store.DEFAULT_KEY)), out)
        self.assertEqual(data, persona_store.load(persona_store.DEFAULT_KEY))

    def test_nothing_loadable_is_reported(self):
        """连默认那份都没有 → 也得说"现在用的是内置空模板"，不能静默。"""
        real = persona_store.path_of
        persona_store.path_of = lambda key: os.path.join(
            ROOT, "no-such-dir", "x.json")
        try:
            data, out = self._capture("whatever")
        finally:
            persona_store.path_of = real
        self.assertIn("读不到", out)
        self.assertEqual(data, persona_store.DEFAULT_PERSONA)

    def test_broken_json_is_reported_and_does_not_raise(self):
        """坏掉的人设文件不能让服务起不来，但必须留下线索。

        直接把 path_of 指到仓库里一个**不是 JSON** 的现成文件（README.md），
        省掉临时文件 —— 这条路径要测的是"解析失败"，不是"写文件"。
        """
        real = persona_store.path_of
        persona_store.path_of = lambda key: os.path.join(ROOT, "README.md")
        try:
            data, out = self._capture("broken")
        finally:
            persona_store.path_of = real
        self.assertIn("解析失败", out)
        self.assertEqual(data, persona_store.DEFAULT_PERSONA)


class TestKeyFromConfig(unittest.TestCase):

    def test_strips_directories_and_extension(self):
        self.assertEqual(
            persona_store.key_from_config({"persona_file": "personas/x.json"}),
            "x")
        self.assertEqual(
            persona_store.key_from_config(
                {"persona_file": "C:\\some\\dir\\y.json"}), "y")

    def test_empty_config_falls_back_to_default_key(self):
        for cfg in ({}, {"persona_file": ""}, None):
            self.assertEqual(persona_store.key_from_config(cfg),
                             persona_store.DEFAULT_KEY)

    def test_path_traversal_is_neutralised(self):
        """persona_file 来自配置，basename 之后不该还能跳出 personas/。"""
        key = persona_store.key_from_config(
            {"persona_file": "../../../../etc/passwd"})
        self.assertNotIn("..", key)
        self.assertNotIn("/", key)
        self.assertNotIn("\\", key)


if __name__ == "__main__":
    unittest.main()
