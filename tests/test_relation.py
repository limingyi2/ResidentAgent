# -*- coding: utf-8 -*-
"""关系定位：不再有两档，改成 persona 里的自由文本字段。

原来写死 friend/partner 二选一，抽出来是为了让人设文件里写"我们是情侣"
能压过 CORE_RULES（它固定缀在最后，权重最高）。但两档枚举永远补不完 ——
"十年老友""正在追她""同居"都不在里头。现在整个概念去掉：
关系就是人设的一个字段，你说多少是多少。

同时验证 CORE_RULES 里保留下来的那两条 —— 原来 friend 和 partner 两个档里
各写了一遍，删档时一起丢了会静默失效，所以特意搬进去了。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "brain"))

import persona_store as ps   # noqa: E402


class TestRelationIsAField(unittest.TestCase):
    """relation 是人设字段，不是 config 里的两档选择"""

    def test_field_is_in_whitelist(self):
        """不在 FIELDS 里的话 App 保存一次就会被静默丢掉。"""
        self.assertIn("relation", ps.FIELDS)

    def test_old_two_tiers_are_gone(self):
        for name in ("RELATION_FRIEND", "RELATION_PARTNER", "RELATIONS"):
            self.assertFalse(hasattr(ps, name), "%s 还留着" % name)

    def test_relation_of_reads_persona_not_config(self):
        import inspect
        src = inspect.getsource(ps.relation_of)
        self.assertIn("persona", src)
        self.assertNotIn("api_config", src)


class TestRelationOf(unittest.TestCase):
    def test_reads_from_persona(self):
        self.assertEqual(ps.relation_of({"relation": "十年老友，各自城市"}),
                         "十年老友，各自城市")

    def test_freetext_is_kept_verbatim(self):
        """写什么留什么 —— 不做归一化、不查白名单。"""
        for v in ("正在追她，还没答应", "同居两年", "她是你妹",
                  "刚认识两周", "随便什么写法都行"):
            self.assertEqual(ps.relation_of({"relation": v}), v)

    def test_empty_falls_back_to_default(self):
        """留空退回默认，不是变成空。

        选这个而不是"真留空"：误操作清空这一栏时有个兜底比没兜底安全 ——
        这跟原来两档"认错时退回 friend"是同一个思路（认错时静默退回最保守的）。
        真不想要约束的人会直接删掉那栏，不会留着空白。
        """
        for p in ({}, {"relation": ""}, {"relation": "   "}, None):
            self.assertEqual(ps.relation_of(p), ps.DEFAULT_RELATION)

    def test_non_string_is_ignored(self):
        """字段被写成数字/列表时别炸，退回默认。"""
        for v in (123, ["a"], {"x": 1}):
            self.assertEqual(ps.relation_of({"relation": v}), ps.DEFAULT_RELATION)


class TestBuildSystemText(unittest.TestCase):
    def _txt(self, **kw):
        p = {"name": "她", "background": "背景", "scene": "场景",
             "personality": "性格"}
        p.update(kw)
        return ps.build_system_text(p)

    def test_relation_appears_in_text(self):
        t = self._txt(relation="你们是十年老友")
        self.assertIn("你们是十年老友", t)
        self.assertIn("【你和他是什么关系】", t)

    def test_no_signature_param(self):
        """第二参数删了 —— 关系不再是外部传进来的档位。"""
        import inspect
        self.assertEqual(len(inspect.signature(ps.build_system_text)
                             .parameters), 1)

    def test_empty_relation_still_has_default(self):
        """空值走默认，所以标题和默认文案都在 —— 这跟"真留空"的预期相反，
        是有意选的行为（见 TestRelationOf.test_empty_falls_back_to_default）。"""
        t = self._txt(relation="")
        self.assertIn("【你和他是什么关系】", t)
        self.assertIn(ps.DEFAULT_RELATION, t)

    def test_relation_goes_before_core_rules(self):
        """关系段夹在 notes 和 CORE_RULES 之间：它是"他们是什么关系"这个事实，
        CORE_RULES 是无论什么关系都成立的底线 —— 底线压在最末尾。"""
        t = self._txt(relation="同居两年")
        self.assertLess(t.index("同居两年"), t.index("【你是谁"))

    def test_core_rules_keeps_shared_constraints(self):
        for frag in ("打断他时换个说法", "不当真承诺"):
            self.assertIn(frag, ps.CORE_RULES)

    def test_core_rules_has_no_tier_specific_text(self):
        """档位专属的那些不能留在 CORE_RULES，否则又变成焊死的。"""
        for frag in ("不是恋人", "不搞暧昧", "在谈的恋人"):
            self.assertNotIn(frag, ps.CORE_RULES)


class TestPersonaFiles(unittest.TestCase):
    """预设已删，只留当前这套 + 空模板"""

    def setUp(self):
        self.d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "brain", "config", "personas")

    def test_no_presets(self):
        left = [f for f in os.listdir(self.d) if f.startswith("preset_")]
        self.assertEqual(left, [], "预设还在：%s" % left)

    def test_template_has_relation_field(self):
        import json
        d = json.load(open(os.path.join(self.d, "default.json"),
                           encoding="utf-8"))
        self.assertIn("relation", d)


class TestApiSurface(unittest.TestCase):
    """App 侧：关系档接口没了，换成人设编辑接口"""

    def setUp(self):
        self.src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "brain", "server.py"),
                        encoding="utf-8").read()

    def test_relation_apply_endpoint_removed(self):
        self.assertNotIn("/api/relation/apply", self.src)

    def test_persona_editor_endpoints_exist(self):
        self.assertIn('"/api/persona/full"', self.src)
        self.assertIn('"/api/persona/save"', self.src)

    def test_full_is_in_get_branch(self):
        """编辑器是用 GET 读人设的（只读）。接口写在 POST 分支里就404。

        这条踩过：接口写对了、内容也部署上去了，但前端GET 就是 404 ——
        查文件在、行号对、语法没问题，最后才发现它挂在 POST 那条链上。
        """
        i = self.src.index('"/api/persona/full"')
        # 往前找最近的分支判断：GET 分支用 "if path =="，POST 用 "elif"
        head = self.src[max(0, i - 1200):i]
        last_if = head.rfind("if path ==")
        last_elif = head.rfind("elif path ==")
        self.assertGreater(last_if, last_elif,
                           "persona/full 挂在 elif（POST）链上，GET 会404")

    def test_save_validates_name(self):
        """空名字不能存 —— 否则她连自己叫什么都不知道。"""
        i = self.src.index('"/api/persona/save"')
        self.assertIn("总得有个名字", self.src[i:i + 2000])

    def test_full_endpoint_guards_path_traversal(self):
        """key 来自查询串，必须 basename + isfile 双重校验。"""
        i = self.src.index('"/api/persona/full"')
        seg = self.src[i:i + 1600]
        self.assertIn("os.path.basename", seg)
        self.assertIn("isfile", seg)

    def test_brain_no_longer_passes_relation(self):
        b = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "brain", "brain.py"), encoding="utf-8").read()
        self.assertNotIn("self.relation", b)
        self.assertNotIn("relation_of", b)


class TestFrontendEditor(unittest.TestCase):
    """App 里有人设编辑器了"""

    def setUp(self):
        self.src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "_chat.html"), encoding="utf-8").read()

    def test_no_relation_tier_ui(self):
        for frag in ("relationList", "relationCur", "applyRelation",
                     "RELATION_LABELS"):
            self.assertNotIn(frag, self.src, frag)

    def test_editor_has_all_fields(self):
        for k in ("name", "nicknames", "relation", "background", "scene",
                  "personality", "notes", "appearance"):
            self.assertIn("k: '%s'" % k, self.src)

    def test_editor_calls_new_endpoints(self):
        self.assertIn("/api/persona/save", self.src)
        self.assertIn("/api/persona/full", self.src)


class TestSaveKeepsUnsentFields(unittest.TestCase):
    """App 编辑器只发 9 个字段，save 不能把其余的清掉。

    label/desc 是列表元数据，编辑器里没有这两栏（用户不该看见它们），
    所以前端永远不会传。原先 save 一律 data.get(k, DEFAULT_PERSONA[k]) 兜底，
    在 App 里保存一次就把它们清成空串，列表里的副标题跟着没了。
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.orig = ps.PERSONA_DIR
        ps.PERSONA_DIR = self.tmp
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, ps, "PERSONA_DIR", self.orig)

    def test_label_desc_survive_a_save(self):
        ps.save({"name": "她", "label": "原版 · 老朋友",
                 "desc": "顺手跟你说两句"}, "k")
        ps.save({"name": "她", "relation": "同居两年"}, "k")
        import json
        d = json.load(open(ps.path_of("k"), encoding="utf-8"))
        self.assertEqual(d["label"], "原版 · 老朋友")
        self.assertEqual(d["desc"], "顺手跟你说两句")
        self.assertEqual(d["relation"], "同居两年")

    def test_empty_string_still_clears(self):
        """传了空串是用户主动清空，要照写 —— 跟"没传"不是一回事。"""
        ps.save({"name": "她", "label": "有值"}, "k")
        ps.save({"name": "她", "label": ""}, "k")
        import json
        d = json.load(open(ps.path_of("k"), encoding="utf-8"))
        self.assertEqual(d["label"], "")

    def test_missing_file_falls_back_to_defaults(self):
        """文件不存在时不能炸，缺的字段用默认。"""
        ps.save({"name": "她"}, "new")
        import json
        d = json.load(open(ps.path_of("new"), encoding="utf-8"))
        self.assertEqual(d["name"], "她")
        self.assertIn("relation", d)


class TestDraftRulesNotBindingRelation(unittest.TestCase):
    """draft_rules 每轮必发，权重比人设字段高 —— 里面不能焊死关系形态"""

    def setUp(self):
        self.rules = ps.build_draft_rules({"call_user": ""})

    def test_no_hardcoded_long_distance(self):
        """关系改成自由文本了，"异地"这种前提不该还写在必发的规则里。"""
        self.assertNotIn("重要：异地", self.rules)

    def test_still_guards_unexpected_meeting_promises(self):
        """不能见面时别随口承诺，但这条得保留（换个说法）。"""
        for frag in ("我陪你", "能不能见面"):
            self.assertIn(frag, self.rules)

    def test_still_blocks_faking_sees_his_room(self):
        """删"异地"那句时别把这条一起带走 —— 它跟异地无关。"""
        self.assertIn("假装看得见他身边", self.rules)


if __name__ == "__main__":
    unittest.main()