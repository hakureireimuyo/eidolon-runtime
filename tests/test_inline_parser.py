"""内联协议解析层测试(零网络)。

覆盖(对齐 docs/streaming-event-loop-placeholder.md §4):
- 普通文本透传 / 片段合并 / 解析后纯文本累积
- 注入:已注册路径解析替换;未注册 / resolver 异常 → 静默替换为空
- 指令:b / i 渐进透传;未知 kind → 静默丢弃
- 截断:未闭合 / 缓冲超限 → 静默丢弃;转义;无嵌套(解析结果不再二次解析)
"""
from __future__ import annotations

import unittest

from runtime.inline import InterpreterRegistry, StreamParser


def _resolver(path, ctx):
    values = {"time": "12:00", "world:time": "07:30", "char:name": "TestBot"}
    return values.get(path)


class TestStreamParserPlain(unittest.TestCase):
    def test_plain_passthrough(self):
        p = StreamParser()
        events = p.feed("你好")
        self.assertEqual([(e.delta, e.style) for e in events], [("你好", "plain")])
        self.assertEqual(p.resolved_text, "你好")
        self.assertEqual(p.segments, [{"text": "你好", "style": "plain"}])

    def test_chunk_merges_segment(self):
        p = StreamParser()
        p.feed("ab")
        p.feed("cd")
        self.assertEqual(p.resolved_text, "abcd")
        self.assertEqual(p.segments, [{"text": "abcd", "style": "plain"}])

    def test_lone_backslash_literal(self):
        p = StreamParser()
        p.feed("a\\b")
        self.assertEqual(p.resolved_text, "a\\b")

    def test_escape_open_close(self):
        p = StreamParser()
        events = p.feed("\\⟦你好\\⟧")
        self.assertEqual(p.resolved_text, "⟦你好⟧")
        self.assertEqual([(e.delta, e.style) for e in events], [
            ("⟦", "plain"), ("你好", "plain"), ("⟧", "plain"),
        ])


class TestInjection(unittest.TestCase):
    def test_resolved(self):
        p = StreamParser(resolver=_resolver)
        events = p.feed("现在⟦time⟧了")
        self.assertEqual([(e.delta, e.style) for e in events], [
            ("现在", "plain"), ("12:00", "plain"), ("了", "plain"),
        ])
        self.assertEqual(p.resolved_text, "现在12:00了")

    def test_namespaced_path(self):
        p = StreamParser(resolver=_resolver)
        p.feed("世界时间⟦world:time⟧")
        self.assertEqual(p.resolved_text, "世界时间07:30")

    def test_chunked_across_feeds(self):
        p = StreamParser(resolver=_resolver)
        p.feed("现在⟦ti")
        p.feed("me⟧了")
        self.assertEqual(p.resolved_text, "现在12:00了")

    def test_unregistered_silent(self):
        """未接入路径(程序未接入)→ 静默替换为空。"""
        p = StreamParser(resolver=_resolver)
        events = p.feed("⟦ghost:x⟧你好")
        self.assertEqual(p.resolved_text, "你好")
        self.assertEqual([(e.delta, e.style) for e in events], [("你好", "plain")])

    def test_no_resolver_all_silent(self):
        """解析层不认识任何程序:无 resolver 时一切注入静默。"""
        p = StreamParser()
        p.feed("a⟦time⟧b")
        self.assertEqual(p.resolved_text, "ab")

    def test_resolver_value_not_reparsed(self):
        """无嵌套、无递归:解析结果不再二次解析。"""
        p = StreamParser(resolver=lambda path, ctx: "⟦time⟧")
        p.feed("⟦a⟧")
        self.assertEqual(p.resolved_text, "⟦time⟧")

    def test_unclosed_silent(self):
        p = StreamParser(resolver=_resolver)
        p.feed("前缀⟦time")
        self.assertEqual(p.resolved_text, "前缀")
        p.finish()
        self.assertEqual(p.resolved_text, "前缀")

    def test_buffer_overflow_silent(self):
        p = StreamParser(max_buffer=8)
        p.feed("⟦" + "a" * 64 + "⟧尾")
        self.assertEqual(p.resolved_text, "尾")

    def test_empty_placeholder_silent(self):
        p = StreamParser(resolver=_resolver)
        p.feed("a⟦⟧b")
        self.assertEqual(p.resolved_text, "ab")


class TestDirective(unittest.TestCase):
    def test_bold(self):
        p = StreamParser()
        events = p.feed("⟦b:加粗⟧")
        self.assertEqual([(e.delta, e.style) for e in events], [("加粗", "bold")])
        self.assertEqual(p.resolved_text, "加粗")  # 样式剥离
        self.assertEqual(p.segments, [{"text": "加粗", "style": "bold"}])

    def test_bold_progressive_across_chunks(self):
        p = StreamParser()
        p.feed("⟦b:加")
        p.feed("粗⟧尾")
        self.assertEqual(p.segments, [
            {"text": "加粗", "style": "bold"},
            {"text": "尾", "style": "plain"},
        ])

    def test_italic(self):
        p = StreamParser()
        p.feed("⟦i:斜体⟧")
        self.assertEqual(p.segments, [{"text": "斜体", "style": "italic"}])

    def test_mixed_styles_merge(self):
        p = StreamParser()
        p.feed("a⟦b:x⟧b⟦i:y⟧c")
        self.assertEqual(p.segments, [
            {"text": "a", "style": "plain"},
            {"text": "x", "style": "bold"},
            {"text": "b", "style": "plain"},
            {"text": "y", "style": "italic"},
            {"text": "c", "style": "plain"},
        ])

    def test_unknown_kind_silent(self):
        p = StreamParser()
        events = p.feed("⟦x:内容⟧尾")
        self.assertEqual(p.resolved_text, "尾")
        self.assertEqual(events, [])

    def test_empty_directive(self):
        p = StreamParser()
        p.feed("⟦b:⟧")
        self.assertEqual(p.resolved_text, "")


class TestInterpreterRegistry(unittest.TestCase):
    def test_register_resolve(self):
        reg = InterpreterRegistry()
        reg.register("time", lambda ctx: "12:00")
        self.assertEqual(reg.resolve("time"), "12:00")

    def test_unregistered_none(self):
        reg = InterpreterRegistry()
        self.assertIsNone(reg.resolve("ghost"))

    def test_resolver_exception_silent(self):
        reg = InterpreterRegistry()
        reg.register("boom", lambda ctx: 1 / 0)
        self.assertIsNone(reg.resolve("boom"))

    def test_invalid_path_rejected(self):
        reg = InterpreterRegistry()
        with self.assertRaises(ValueError):
            reg.register(" bad ", lambda ctx: "x")

    def test_paths_sorted(self):
        reg = InterpreterRegistry()
        reg.register("b", lambda ctx: "1")
        reg.register("a", lambda ctx: "2")
        self.assertEqual(reg.paths(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
