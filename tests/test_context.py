"""Context Management 抽象层测试(零网络)。

覆盖:
- ContextLayer 层级排序(static → high)
- ContextSegment / ContextIR 分组、替换、查询
- ConversationBuffer 追加、截断、导出
- ContextCompiler 编译为 messages(缓存友好布局)
- ContextManager 分层管理 + 增量更新 + 编译
"""
from __future__ import annotations

import unittest

from runtime.context import (
    ContextManager,
    ContextCompiler,
    ContextIR,
    ContextLayer,
    ContextSegment,
    ConversationBuffer,
    ConversationTurn,
)


class TestContextLayer(unittest.TestCase):
    def test_ordering(self):
        self.assertLess(ContextLayer.STATIC, ContextLayer.LOW)
        self.assertLess(ContextLayer.LOW, ContextLayer.MID)
        self.assertLess(ContextLayer.MID, ContextLayer.HIGH)

    def test_labels(self):
        self.assertEqual(ContextLayer.STATIC.label, "静态层")
        self.assertEqual(ContextLayer.HIGH.label, "高频层")


class TestContextSegment(unittest.TestCase):
    def test_creation(self):
        seg = ContextSegment(
            text="hello", layer=ContextLayer.STATIC, tag="test"
        )
        self.assertEqual(seg.text, "hello")
        self.assertEqual(seg.role, "system")
        self.assertTrue(seg.cacheable)

    def test_empty_tag_rejected(self):
        with self.assertRaises(ValueError):
            ContextSegment(text="x", layer=ContextLayer.STATIC, tag="")

    def test_non_system_role(self):
        seg = ContextSegment(
            text="user input",
            layer=ContextLayer.HIGH,
            tag="user_msg",
            role="user",
        )
        self.assertEqual(seg.role, "user")


class TestContextIR(unittest.TestCase):
    def test_add_and_find(self):
        ir = ContextIR()
        seg = ContextSegment(text="world", layer=ContextLayer.STATIC, tag="world")
        ir.add(seg)
        found = ir.find("world")
        self.assertIsNotNone(found)
        self.assertEqual(found.text, "world")

    def test_replace_tag(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="v1", layer=ContextLayer.STATIC, tag="x"))
        ir.replace_tag("x", ContextSegment(text="v2", layer=ContextLayer.STATIC, tag="x"))
        self.assertEqual(ir.find("x").text, "v2")

    def test_remove_tag(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="a", layer=ContextLayer.STATIC, tag="a"))
        ir.add(ContextSegment(text="b", layer=ContextLayer.LOW, tag="b"))
        ir.remove_tag("a")
        self.assertIsNone(ir.find("a"))
        self.assertIsNotNone(ir.find("b"))

    def test_by_layer(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="s", layer=ContextLayer.STATIC, tag="s"))
        ir.add(ContextSegment(text="h", layer=ContextLayer.HIGH, tag="h"))
        ir.add(ContextSegment(text="m", layer=ContextLayer.MID, tag="m"))
        groups = ir.by_layer()
        self.assertEqual(len(groups[ContextLayer.STATIC]), 1)
        self.assertEqual(len(groups[ContextLayer.HIGH]), 1)
        self.assertEqual(len(groups[ContextLayer.LOW]), 0)

    def test_sorted_segments_stable_order(self):
        """按稳定性排序,同层保持插入顺序。"""
        ir = ContextIR()
        ir.add(ContextSegment(text="h2", layer=ContextLayer.HIGH, tag="h2"))
        ir.add(ContextSegment(text="s1", layer=ContextLayer.STATIC, tag="s1"))
        ir.add(ContextSegment(text="h1", layer=ContextLayer.HIGH, tag="h1"))
        ir.add(ContextSegment(text="m1", layer=ContextLayer.MID, tag="m1"))
        sorted_segs = ir.sorted_segments()
        tags = [s.tag for s in sorted_segs]
        self.assertEqual(tags, ["s1", "m1", "h2", "h1"])

    def test_total_text_length(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="abc", layer=ContextLayer.STATIC, tag="a"))
        ir.add(ContextSegment(text="de", layer=ContextLayer.LOW, tag="b"))
        self.assertEqual(ir.total_text_length, 5)

    def test_len_and_iter(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="a", layer=ContextLayer.STATIC, tag="a"))
        ir.add(ContextSegment(text="b", layer=ContextLayer.LOW, tag="b"))
        self.assertEqual(len(ir), 2)
        tags = [s.tag for s in ir]
        self.assertEqual(tags, ["a", "b"])


class TestConversationBuffer(unittest.TestCase):
    def test_add_and_export(self):
        buf = ConversationBuffer()
        buf.add("user", "hello")
        buf.add("assistant", "hi there")
        msgs = buf.to_messages()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0], {"role": "user", "content": "hello"})
        self.assertEqual(msgs[1], {"role": "assistant", "content": "hi there"})

    def test_eviction(self):
        buf = ConversationBuffer(max_turns=3)
        buf.add("user", "m1")
        buf.add("assistant", "r1")
        buf.add("user", "m2")
        buf.add("assistant", "r2")
        # 4 turns, max 3 → oldest evicted
        self.assertEqual(len(buf), 3)
        self.assertEqual(buf.turns[0].content, "r1")

    def test_clear(self):
        buf = ConversationBuffer()
        buf.add("user", "x")
        buf.clear()
        self.assertEqual(len(buf), 0)

    def test_invalid_role(self):
        buf = ConversationBuffer()
        with self.assertRaises(ValueError):
            buf.add("invalid_role", "x")

    def test_max_turns_setter_triggers_eviction(self):
        buf = ConversationBuffer(max_turns=10)
        for i in range(5):
            buf.add("user", f"m{i}")
        buf.max_turns = 2
        self.assertEqual(len(buf), 2)
        # 只保留最新 2 条
        self.assertEqual(buf.turns[0].content, "m3")
        self.assertEqual(buf.turns[1].content, "m4")


class TestContextCompiler(unittest.TestCase):
    def test_compile_system_only(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="rule1", layer=ContextLayer.STATIC, tag="r1"))
        ir.add(ContextSegment(text="rule2", layer=ContextLayer.LOW, tag="r2"))
        compiler = ContextCompiler()
        msgs = compiler.compile(ir)
        # 两条 system 片段合并为一条 system message
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("rule1", msgs[0]["content"])
        self.assertIn("rule2", msgs[0]["content"])

    def test_compile_with_conversation(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="system prompt", layer=ContextLayer.STATIC, tag="sp"))
        buf = ConversationBuffer()
        buf.add("user", "hi")
        buf.add("assistant", "hello")
        compiler = ContextCompiler()
        msgs = compiler.compile(ir, buf)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("system prompt", msgs[0]["content"])
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[2]["role"], "assistant")

    def test_compile_prefix_excludes_high(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="static", layer=ContextLayer.STATIC, tag="s"))
        ir.add(ContextSegment(text="mid", layer=ContextLayer.MID, tag="m"))
        ir.add(
            ContextSegment(
                text="emotion", layer=ContextLayer.HIGH, tag="e"
            )
        )
        compiler = ContextCompiler()
        prefix = compiler.compile_prefix(ir)
        self.assertIn("static", prefix)
        self.assertIn("mid", prefix)
        self.assertNotIn("emotion", prefix)

    def test_compile_order_stable_to_dynamic(self):
        """编译后 system message 中片段按稳定性排序。"""
        ir = ContextIR()
        ir.add(ContextSegment(text="HIGH", layer=ContextLayer.HIGH, tag="h"))
        ir.add(ContextSegment(text="LOW", layer=ContextLayer.LOW, tag="l"))
        ir.add(ContextSegment(text="STATIC", layer=ContextLayer.STATIC, tag="s"))
        ir.add(ContextSegment(text="MID", layer=ContextLayer.MID, tag="m"))
        compiler = ContextCompiler()
        msgs = compiler.compile(ir)
        content = msgs[0]["content"]
        # STATIC 应该在 LOW 之前
        self.assertLess(content.index("STATIC"), content.index("LOW"))
        self.assertLess(content.index("LOW"), content.index("MID"))
        self.assertLess(content.index("MID"), content.index("HIGH"))

    def test_non_system_segments_as_messages(self):
        """非 system 角色的片段直接作为独立 message。"""
        ir = ContextIR()
        ir.add(
            ContextSegment(
                text="user msg", layer=ContextLayer.HIGH, tag="u1", role="user"
            )
        )
        ir.add(
            ContextSegment(
                text="assistant msg",
                layer=ContextLayer.HIGH,
                tag="a1",
                role="assistant",
            )
        )
        compiler = ContextCompiler()
        msgs = compiler.compile(ir)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")

    def test_estimate_cache_boundary(self):
        ir = ContextIR()
        ir.add(ContextSegment(text="s", layer=ContextLayer.STATIC, tag="static_tag"))
        ir.add(ContextSegment(text="m", layer=ContextLayer.MID, tag="mid_tag"))
        ir.add(
            ContextSegment(
                text="u", layer=ContextLayer.HIGH, tag="user_tag", role="user"
            )
        )
        buf = ConversationBuffer()
        buf.add("user", "x")
        buf.add("assistant", "y")
        compiler = ContextCompiler()
        info = compiler.estimate_cache_boundary(ir, buf)
        self.assertIn("static_tag", info["prefix_segments"])
        self.assertIn("mid_tag", info["prefix_segments"])
        self.assertIn("user_tag", info["dynamic_segments"])
        self.assertEqual(info["conversation_turns"], 2)


class TestContextManager(unittest.TestCase):
    def test_set_static_and_compile(self):
        mgr = ContextManager()
        mgr.set_static("char_prompt", "你是一个 AI 角色")
        mgr.add_message("user", "你好")
        msgs = mgr.compile()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("你是一个 AI 角色", msgs[0]["content"])
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "你好")

    def test_incremental_update(self):
        """更新一个片段不重建其他片段。"""
        mgr = ContextManager()
        mgr.set_static("a", "text_a_v1")
        mgr.set_static("b", "text_b")
        msgs1 = mgr.compile()
        # 更新 a
        mgr.set_static("a", "text_a_v2")
        msgs2 = mgr.compile()
        # b 不变
        self.assertIn("text_b", msgs2[0]["content"])
        # a 变化
        self.assertNotIn("text_a_v1", msgs2[0]["content"])
        self.assertIn("text_a_v2", msgs2[0]["content"])

    def test_multi_layer_compile(self):
        mgr = ContextManager()
        mgr.set_static("world", "世界观")
        mgr.set_low("time", "现在是冬天")
        mgr.set_mid("relationship", "信任度: 0.5")
        mgr.add_message("user", "在吗")
        msgs = mgr.compile()
        self.assertEqual(len(msgs), 2)  # 1 system + 1 user
        system_content = msgs[0]["content"]
        # 按 layer 顺序排列
        self.assertLess(system_content.index("世界观"), system_content.index("冬天"))
        self.assertLess(system_content.index("冬天"), system_content.index("信任度"))

    def test_reset_conversation_preserves_static(self):
        mgr = ContextManager()
        mgr.set_static("prompt", "system")
        mgr.add_message("user", "m1")
        mgr.add_message("assistant", "r1")
        mgr.reset_conversation()
        msgs = mgr.compile()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")

    def test_clear_all(self):
        mgr = ContextManager()
        mgr.set_static("x", "a")
        mgr.add_message("user", "b")
        mgr.clear_all()
        msgs = mgr.compile()
        self.assertEqual(len(msgs), 0)

    def test_compile_prefix_cache(self):
        mgr = ContextManager()
        mgr.set_static("s", "static text")
        mgr.set_mid("m", "mid text")
        prefix = mgr.compile_prefix()
        self.assertIn("static text", prefix)
        self.assertIn("mid text", prefix)

    def test_cache_info(self):
        mgr = ContextManager()
        mgr.set_static("s1", "static")
        mgr.set_high("emotion", "开心")
        mgr.add_message("user", "hi")
        info = mgr.cache_info()
        self.assertIn("s1", info["prefix_segments"])
        self.assertIn("emotion", info["dynamic_segments"])
        self.assertEqual(info["conversation_turns"], 1)

    def test_remove_segment(self):
        mgr = ContextManager()
        mgr.set_static("a", "text_a")
        mgr.set_static("b", "text_b")
        mgr.remove("a")
        msgs = mgr.compile()
        self.assertNotIn("text_a", msgs[0]["content"])
        self.assertIn("text_b", msgs[0]["content"])

    def test_get_segment(self):
        mgr = ContextManager()
        mgr.set_static("x", "val")
        seg = mgr.get_segment("x")
        self.assertIsNotNone(seg)
        self.assertEqual(seg.text, "val")
        self.assertEqual(seg.layer, ContextLayer.STATIC)

    def test_conversation_max_turns_configurable(self):
        mgr = ContextManager(max_conversation_turns=2)
        mgr.add_message("user", "m1")
        mgr.add_message("assistant", "r1")
        mgr.add_message("user", "m2")
        # max 2 turns → eviction
        self.assertEqual(len(mgr.conversation_turns), 2)


if __name__ == "__main__":
    unittest.main()
