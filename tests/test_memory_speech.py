# -*- coding: utf-8 -*-
"""记忆的三道新约束：她说的话要不要记、过期记忆要不要压、主动搭话算不算数。

起因是实测发现她在 07:40 主动搭话时说过「下午那个高铁，你去接我啊」—— 841 条
历史里从没这回事，是她编的。然后 11:57 她自己忘了，还倒打一耙说用户记错了。

查下来根因有三处，都是"记忆库本身没问题、但它收到的输入不对"：
  1. 抽取只喂了用户的话（brain.py 里 extract(user_text)），她说的承诺从来不记
  2. 主动搭话整轮不抽取（if not proactive），她自己冒出来的话零记录
  3. 记忆只增不减，过期 8 天的「25号她坐车去找他」还躺在库里会被当成即将发生
"""
import os
import sys
import datetime
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "brain"))

import memory_store_v2 as M   # noqa: E402


class TestClassifyHerSpeech(unittest.TestCase):
    """classify_her_speech：她说的话哪些该进记忆库"""

    def test_promise_is_saved(self):
        ok, why = M.classify_her_speech("我明天还得去望江楼拍银杏呢")
        self.assertTrue(ok)
        self.assertEqual(why, "promise")

    def test_hypothetical_is_not_saved(self):
        """这条是实测里她真说过的话（原话）。存进去下次她会"记得"有个约定。"""
        ok, why = M.classify_her_speech(
            "要是无聊，高铁三小时过来找我也行，望江楼溜达去。")
        self.assertFalse(ok)
        self.assertEqual(why, "hypothetical")

    def test_forecast_is_not_saved(self):
        """"你那边17度，出门记得穿外套" 有"记得"这个词，但不是承诺。"""
        ok, why = M.classify_her_speech("你那边阴天17度，出门记得穿外套。")
        self.assertFalse(ok)
        self.assertEqual(why, "forecast")

    def test_past_tense_is_not_saved(self):
        """"我困死了" / "放假是真爽" —— 讲当下感受或评价，不是安排。"""
        for t in ("我困死了", "放假是真爽。", "我今天有点累", "我今天状态不错"):
            ok, _ = M.classify_her_speech(t)
            self.assertFalse(ok, t)

    def test_joking_is_not_saved(self):
        ok, why = M.classify_her_speech("开玩笑的，我明天去")
        self.assertFalse(ok)
        self.assertEqual(why, "joking")

    def test_chitchat_is_not_saved(self):
        for t in ("我困死了", "放假是真爽。", "随便你", "我还真忘了，光顾着睡。"):
            ok, _ = M.classify_her_speech(t)
            self.assertFalse(ok, t)

    def test_hypothetical_beats_commit_marker(self):
        """"要是我明天下午去，你就别等了" 既含"我…去"也含"要是"。假设优先。"""
        ok, why = M.classify_her_speech("要是我明天下午去接你，你就别等了")
        self.assertFalse(ok)
        self.assertEqual(why, "hypothetical")

    def test_state_description_with_time_word_is_not_saved(self):
        """有时间词但整句在讲状态 —— 不是安排。

        "我今天状态不错" 含"我"和"今天"，看着像承诺，实际是要记成"她今天状态不错"
        这种废话。状态短语要判在第一人称之前，否则会被放行。
        """
        for t in ("我今天状态不错", "我今天有点累", "我今天心情不好"):
            ok, _ = M.classify_her_speech(t)
            self.assertFalse(ok, t)

    def test_vague_plan_is_saved(self):
        """"我明天有点事" 没说什么事，但确实是个安排，值得记。

        这里原本断言不该记，写错了 —— 她说了"明天有事"，下次提起是有用的。
        """
        ok, why = M.classify_her_speech("我明天有点事")
        self.assertTrue(ok)
        self.assertEqual(why, "promise")

    def test_state_word_does_not_kill_real_promise(self):
        """这条是回归：曾经用单词"困"判状态，把真承诺误杀了。

        "我明天还得去望江楼拍银杏呢，困死了" 里的"困"是修饰，不是整句在讲状态。
        """
        ok, why = M.classify_her_speech("我明天还得去望江楼拍银杏呢，困死了")
        self.assertTrue(ok)
        self.assertEqual(why, "promise")


class TestDueDate(unittest.TestCase):
    """_due_date：从记忆文本里解析截止日"""

    def test_bare_day_uses_write_time_as_anchor(self):
        """这条是核心。

        9-22 写下的「25号她坐车去找他」说的是 9-25。到 10-03 再解析，如果只看
        当前日期，"25号"永远解析成未来的 10-25，那条过期 8 天的记忆就永远降不了权
        —— 这是实测里第一版就错的（裸日号被我写成"往后顺延"）。
        """
        due = M._due_date("25号她坐车去找他",
                          written=datetime.datetime(2026, 9, 22, 16, 35))
        self.assertEqual(due.strftime("%Y-%m-%d"), "2026-09-25")

    def test_absolute_month_day(self):
        due = M._due_date("9月25号她坐车来找他",
                          written=datetime.datetime(2026, 9, 22, 16, 35))
        self.assertEqual(due.strftime("%Y-%m-%d"), "2026-09-25")

    def test_relative_day(self):
        due = M._due_date("明天上午10点提醒起床学习",
                          written=datetime.datetime(2026, 10, 2, 3, 26))
        self.assertEqual(due.strftime("%Y-%m-%d"), "2026-10-03")

    def test_no_date_returns_none(self):
        """解析不出来就返回 None —— 猜错日期比不猜更糟。"""
        for t in ("用户已订好酒店", "计划去吃烤肉自助", "喜好：喜欢猫"):
            self.assertIsNone(M._due_date(t, written=datetime.datetime.now()))

    def test_ambiguous_month_is_not_guessed(self):
        """"13月" 不存在，不能瞎猜。"""
        self.assertIsNone(M._due_date("13月5号有事",
                                      written=datetime.datetime(2026, 9, 22)))


class TestWeight(unittest.TestCase):
    """_weight：过期与存疑记忆降权"""

    def _item(self, text, when, **kw):
        d = {"text": text, "type": "event",
             "time": when.strftime("%Y-%m-%d %H:%M")}
        d.update(kw)
        return d

    def test_expired_event_is_downweighted(self):
        it = self._item("中秋安排最终版：25号她坐车去找他",
                        datetime.datetime(2026, 9, 22, 16, 35))
        self.assertLess(M._weight(it), 1.0)

    def test_expired_drops_below_render_threshold(self):
        """过期 8 天 × 检索 0.62 分 → 乘权后要低于 0.40 阈值，自动不注入。

        这是实测数据算出来的，不是估的。
        """
        it = self._item("中秋安排最终版：25号她坐车去找他",
                        datetime.datetime(2026, 9, 22, 16, 35))
        self.assertLess(M._weight(it) * 0.62, 0.40)

    def test_fact_without_date_is_untouched(self):
        """事实类（喜好/习惯/身份）不该被日期逻辑波及。"""
        it = self._item("舍友习惯：舍友在天气较冷时仍开空调",
                        datetime.datetime(2026, 9, 21, 22, 51))
        self.assertEqual(M._weight(it), 1.0)

    def test_future_event_is_untouched(self):
        it = self._item("明天上午10点提醒起床学习",
                        datetime.datetime(2026, 10, 2, 3, 26))
        self.assertEqual(M._weight(it), 1.0)

    def test_proactive_source_is_downweighted(self):
        """主动搭话自己冒出来的承诺多半是编的，压一档。"""
        it = self._item("明天下午去接他", datetime.datetime(2026, 10, 3, 7, 40),
                        src="proactive")
        self.assertLess(M._weight(it), 1.0)

    def test_weight_never_negative(self):
        it = self._item("25号她坐车去找他", datetime.datetime(2026, 9, 22),
                        src="proactive")
        self.assertGreater(M._weight(it), 0)


class TestAgendaIsTheAuthority(unittest.TestCase):
    """过期判定必须以 agenda 为准，不能在 memory 侧再解析一遍日期。

    实测的漂移：agenda.json 里 9 条已盖 missed，memory.json 里同样内容还是满权重。
    她照样把「25号她坐车去找他」当即将发生提。
    """

    def setUp(self):
        import tempfile
        self._d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self._d, "memory"), exist_ok=True)
        with open(os.path.join(self._d, "memory", "memory.json"), "w",
                  encoding="utf-8") as f:
            f.write('{"items": []}')
        import json
        with open(os.path.join(self._d, "agenda.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"items": [
                {"text": "25号放假", "date": "2026-09-25", "status": "missed"},
                {"text": "明天有个面试", "date": "2099-01-01", "status": ""},
            ]}, f)
        self._old = M.configured_mem_dir
        M.configured_mem_dir = lambda: os.path.join(self._d, "memory")
        M._AGENDA_MISSED = None
        M._AGENDA_MTIME = 0.0

    def tearDown(self):
        M.configured_mem_dir = self._old
        M._AGENDA_MISSED = None
        M._AGENDA_MTIME = 0.0

    def test_missed_from_agenda_is_read(self):
        self.assertIn("25号放假", M._agenda_missed_texts())

    def test_future_not_in_missed(self):
        self.assertNotIn("明天有个面试", M._agenda_missed_texts())

    def test_missed_item_gets_expired_weight(self):
        it = {"text": "25号放假", "type": "event",
              "time": "2026-09-22 16:09"}
        self.assertLess(M._weight(it), 0.5)

    def test_cache_is_invalidated_on_mtime(self):
        """改了 agenda.json 得重读，别拿缓存的旧判定。"""
        import json
        M._agenda_missed_texts()
        p = os.path.join(self._d, "agenda.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"items": []}, f)
        os.utime(p, None)          # 确保 mtime 变化
        self.assertEqual(M._agenda_missed_texts(), set())


class TestRetrieveDegradeReturnsTuples(unittest.TestCase):
    """降级路径的返回值形状必须和正常路径一致。

    实测：embedding 挂掉时 retrieve 返回纯文本列表，render 按 (score, text)
    解包直接 ValueError —— 服务最需要降级的时候崩在降级里。
    """

    def test_degrade_branch_returns_tuples(self):
        import inspect
        src = inspect.getsource(M.MemoryStore.retrieve)
        self.assertNotIn('return [i["text"] for i', src,
                         "降级返回了纯文本，render 解包会崩")
        self.assertIn("return [(0.30,", src)


class TestRenderOrdering(unittest.TestCase):
    """注入顺序：正常记忆在前，过期的压到末尾。"""

    def setUp(self):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        self.store = M.MemoryStore(os.path.join(d, "memory.json"))
        with self.store._lock:
            self.store.data["items"] = [
                {"id": "a", "type": "event", "text": "今天学习了Python一天",
                 "time": "2026-09-30 18:18"},
                {"id": "b", "type": "event", "text": "明天下午去接他",
                 "time": "2026-10-03 07:40"},
                {"id": "c", "type": "fact", "text": "用户喜欢猫",
                 "time": "2026-09-23 00:36"},
            ]

    def test_expired_goes_last(self):
        out = self.store.render(query=None, max_items=10)
        segs = [s.strip() for s in out.split("\n")[1].split("；")]
        self.assertEqual(segs[-1], "今天学习了Python一天",
                         "过期的应排在最后（模型对末尾更敏感）")
        self.assertNotIn("今天学习了Python一天", segs[:2])

    def test_normal_items_keep_recency_order(self):
        """权重相同的组内保持原时间顺序（新的在前），别被 sort 打乱。"""
        out = self.store.render(query=None, max_items=10)
        segs = [s.strip() for s in out.split("\n")[1].split("；")]
        self.assertEqual(segs[:2], ["明天下午去接他", "用户喜欢猫"])




class TestAddSrcField(unittest.TestCase):
    """add() 的 src 参数：记来源，不写进 text（写进去会污染检索）"""

    def test_src_is_optional(self):
        self.assertIn("def add(self, text, mtype=\"event\", src=\"\")",
                      open(M.__file__, encoding="utf-8").read())

    def test_src_stored_on_item(self):
        import inspect
        src = inspect.getsource(M.MemoryStore.add)
        self.assertIn('item["src"] = src', src)
        # src 只能在 src 非空时写，别给老条目塞一堆空字段
        self.assertIn("if src:", src)


class TestHerPromiseExtract(unittest.TestCase):
    """extract_her_promise：主语视角 + 第三人称兜底"""

    def setUp(self):
        self._ac = {"api_base": "http://x", "api_key": "k", "model": "m"}
        self._post = M.httpx.post

        class R:
            def __init__(s, t):
                s.t = t

            def raise_for_status(s):
                pass

            def json(s):
                return {"choices": [{"message": {"content": s.t}}]}

        def mk(payload):
            def post(url, headers=None, json=None, timeout=None):
                return R(payload)
            return post

        self.mk = mk

    def tearDown(self):
        M.httpx.post = self._post

    def test_prompt_is_third_person(self):
        """提示词里必须写明第三人称，且禁止自称助手。"""
        import inspect
        src = inspect.getsource(M.MemoryStore.extract_her_promise)
        self.assertIn("第三人称", src)
        self.assertIn("助手", src)

    def test_assistant_self_reference_is_fixed(self):
        """实测抽出来过「地点由我（助手）决定」—— 提示词不够，得代码兜底。"""
        M.httpx.post = self.mk(
            '{"events": ["国庆回来后与用户见面，地点由我（助手）决定"]}')
        ev, _, _ = M.MemoryStore.extract_her_promise("x", self._ac)
        self.assertTrue(ev)
        self.assertNotIn("助手", ev[0])
        self.assertNotIn("（", ev[0])

    def test_first_person_is_rewritten(self):
        M.httpx.post = self.mk('{"events": ["明天我来接他"]}')
        ev, _, _ = M.MemoryStore.extract_her_promise("x", self._ac)
        self.assertEqual(ev[0], "明天她来接他")

    def test_correct_third_person_untouched(self):
        M.httpx.post = self.mk('{"events": ["她明天下午在望江楼拍银杏"]}')
        ev, _, _ = M.MemoryStore.extract_her_promise("x", self._ac)
        self.assertEqual(ev[0], "她明天下午在望江楼拍银杏")

    def test_empty_is_valid(self):
        """不是约定就该返回空，不是失败。"""
        M.httpx.post = self.mk('{"events": []}')
        ev, _, mode = M.MemoryStore.extract_her_promise("我困死了", self._ac)
        self.assertEqual(ev, [])
        self.assertEqual(mode, "api")


class TestFixThirdPerson(unittest.TestCase):
    """_fix_third_person：两侧都要过，且方向不能弄反

    实测踩过：原来只给承诺侧加兜底，用户侧抽出来的「地点由我（助手）决定」照样入库。
    后来修成无脑把「我」全换成「她」，又会把用户的事写成她的（库里有条
    「今天学习了Python一天」是用户的，主语不能反）。
    """

    def test_both_sides_pass_through(self):
        import inspect
        src_a = inspect.getsource(M.MemoryStore.extract_with_api)
        src_b = inspect.getsource(M.MemoryStore.extract_her_promise)
        self.assertIn("_fix_third_person", src_a, "用户侧没过兜底")
        self.assertIn("_fix_third_person", src_b)

    def test_subject_direction_differs(self):
        """用户侧换「他」，承诺侧换「她」—— 反了就把用户的事写成她的。"""
        t = "国庆回来后与用户见面，地点由我（助手）决定"
        self.assertIn("由他决定", M._fix_third_person(t, "他"))
        self.assertIn("由她决定", M._fix_third_person(t, "她"))

    def test_bare_first_person_is_left_alone(self):
        """"我今天学习了Python一天" 说的是用户自己，不该换主语。

        抽取结果描述的是「谁做了什么」，主语归属由模型定，代码只管清理
        第三方口吻（「我（助手）」）和自称代词（「我来」→「他来」）。
        """
        for t in ("我今天学习了Python一天", "我明天下午三点去看书"):
            self.assertEqual(M._fix_third_person(t, "他"), t)

    def test_self_reference_is_fixed(self):
        self.assertEqual(M._fix_third_person("明天我来接他", "他"), "明天他来接他")
        self.assertEqual(M._fix_third_person("我来接你", "她"), "她来接你")

    def test_fact_text_untouched(self):
        for t in ("喜好：喜欢猫", "用户想应聘某家公司", "她和他国庆在公园见面。"):
            self.assertEqual(M._fix_third_person(t, "他"), t)


class TestBrainWiring(unittest.TestCase):
    """brain.py 接线：主动搭话也抽取、剥前缀只做一次"""

    def setUp(self):
        self.src = open(
            os.path.join(os.path.dirname(__file__), "..", "brain", "brain.py"),
            encoding="utf-8").read()

    def test_proactive_no_longer_skips_extraction(self):
        self.assertNotIn("if self.mem is not None and not proactive:",
                         self.src,
                         "主动搭话整轮不抽取，她自己冒出来的话零记录")

    def test_her_speech_is_extracted(self):
        self.assertIn("extract_her_promise", self.src)
        self.assertIn("classify_her_speech", self.src)

    def test_prefix_stripped_once(self):
        """剥「说 xxx」前缀原来在两处各做一遍。"""
        self.assertEqual(self.src.count('startswith("说 ")'), 1,
                         "剥前缀的代码重复了")

    def test_her_text_not_stored_in_history(self):
        """她的承诺进记忆库，但不能当用户的话进历史。"""
        self.assertIn("her_speech", self.src)


if __name__ == "__main__":
    unittest.main()