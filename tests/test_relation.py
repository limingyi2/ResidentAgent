# -*- coding: utf-8 -*-
"""关系档（friend / partner）的行为约束。

这一层是独立维度：7 套性格只管语气，关系是另一回事。测试盯三件事 ——
切档真的换文案、非法值退回保守档、两档互不污染。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "brain"))

import persona_store as ps


P = {"name": "测试", "background": "背景", "scene": "场景",
     "personality": "性格", "notes": "备注", "call_user": ""}


class TestRelationPick(unittest.TestCase):
    def test_显式值优先(self):
        self.assertEqual(ps.relation_of({"relation": "partner"}), "partner")
        self.assertEqual(ps.relation_of({"relation": "friend"}), "friend")

    def test_大小写与空格不敏感(self):
        # App 那边拼错一个空格就静默退回 friend，手输大写也该认
        self.assertEqual(ps.relation_of({"relation": " PARTNER "}), "partner")

    def test_没配或配错都退回朋友(self):
        # 退回"朋友"而不是崩溃：认错时最保守那档才安全
        for cfg in ({}, {"relation": ""}, {"relation": "恋人"},
                    {"relation": None}, {"relation": "partnerish"}):
            self.assertEqual(ps.relation_of(cfg), "friend", cfg)

    def test_配置不是字典也不炸(self):
        self.assertEqual(ps.relation_of(None), "friend")
        self.assertEqual(ps.relation_of([]), "friend")


class TestRelationText(unittest.TestCase):
    def test_两档文案不同(self):
        f = ps.build_system_text(P, "friend")
        r = ps.build_system_text(P, "partner")
        self.assertNotEqual(f, r)
        self.assertIn("不是恋人", f)
        self.assertNotIn("不是恋人", r)

    def test_伴侣档不再残留朋友那句(self):
        # 之前"不是恋人"写在 CORE_RULES 里且权重最高，切到伴侣也会被压回去。
        # 这条是那次改动的核心断言，别删
        txt = ps.build_system_text(P, "partner")
        for s in ("不是恋人", "不搞暧昧", "不用亲昵称呼"):
            self.assertNotIn(s, txt, s)

    def test_朋友档仍然保留原约束(self):
        txt = ps.build_system_text(P, "friend")
        for s in ("不是恋人", "不搞暧昧"):
            self.assertIn(s, txt, s)

    def test_不传关系按朋友处理(self):
        self.assertEqual(ps.build_system_text(P),
                         ps.build_system_text(P, "friend"))

    def test_传了不认的值也退回朋友(self):
        # 拼错不该让关系整段消失 —— 那样她既不是朋友也不是恋人，只剩一堆底线
        self.assertEqual(ps.build_system_text(P, "乱写"),
                         ps.build_system_text(P, "friend"))

    def test_关系段在身份底线之前(self):
        # CORE_RULES 固定缀在最后、末尾权重最高。关系段插在它前面，
        # 顺序反了"她是谁"的底线会盖掉"她俩什么关系"这个事实
        txt = ps.build_system_text(P, "partner")
        self.assertLess(txt.index("在谈的恋人"), txt.index("【你是谁"))

    def test_身份底线仍在一段里(self):
        # 抽关系段的时候别把 CORE_RULES 整段挪丢
        for r in ("friend", "partner"):
            txt = ps.build_system_text(P, r)
            self.assertIn("【你是谁", txt)
            self.assertIn("【你知道什么、不知道什么】", txt)


class TestRelationIndependence(unittest.TestCase):
    def test_CORE_RULES_不再含关系段(self):
        # 关系写死在 CORE_RULES 里是这次要修的病本身
        self.assertNotIn("不是恋人", ps.CORE_RULES)

    def test_关系与性格互不影响(self):
        # 2 关系 x 7 性格 = 14 组合，全靠这两段文本互不干扰
        a = ps.build_system_text(dict(P, personality="性格A"), "friend")
        b = ps.build_system_text(dict(P, personality="性格B"), "friend")
        self.assertIn("性格A", a)
        self.assertIn("性格B", b)
        self.assertNotIn("性格B", a)


if __name__ == "__main__":
    unittest.main()
