"""LLM Gateway 流式事件流测试(零网络)。

覆盖:
- stream_events:文本增量透传 + 工具调用片段组装(按 index)+ done
- 组装:arguments JSON 解析 / 坏 JSON 降级空参数
- 降级:底层服务无 chat_stream → 单块完整文本 + done(旧行为兼容)
- complete_stream:只透传文本分块(兼容接口)
"""
from __future__ import annotations

import unittest

from runtime.llm.base import AIService, ProviderChunk
from runtime.llm_gateway import LLMGateway, LLMRequest


def _text(s):
    return ProviderChunk(kind="text", delta=s)


def _tool(idx, call_id, name, args):
    return ProviderChunk(
        kind="tool_call",
        delta=args,
        tool_call_index=idx,
        tool_call_id=call_id,
        tool_call_name=name,
    )


def _finish(reason="stop"):
    return ProviderChunk(kind="finish", finish_reason=reason)


class _StreamService(AIService):
    name = "streamy"

    def __init__(self, chunks, tools_seen=None):
        self.chunks = chunks
        self.tools_seen = tools_seen

    def chat(self, messages, *, stream=False):
        return "full"

    def chat_stream(self, messages, *, tools=None):
        if self.tools_seen is not None:
            self.tools_seen.append(tools)
        yield from self.chunks


class _PlainService(AIService):
    """只有 chat、无 chat_stream 的服务(降级路径)。"""

    name = "plain"

    def chat(self, messages, *, stream=False):
        return "plain-full"


class TestStreamEvents(unittest.TestCase):
    def test_text_passthrough(self):
        svc = _StreamService([_text("你"), _text("好"), _finish()])
        gw = LLMGateway(service=svc)
        events = list(
            gw.stream_events(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        self.assertEqual(
            [(e.kind, e.delta) for e in events],
            [("text", "你"), ("text", "好"), ("done", "")],
        )
        self.assertEqual(events[-1].finish_reason, "stop")

    def test_tool_call_assembly(self):
        svc = _StreamService([
            _tool(0, "call_1", "get_weather", '{"loc'),
            _tool(0, None, None, 'ation":"杭州"}'),
            _finish("tool_calls"),
        ])
        gw = LLMGateway(service=svc)
        events = list(
            gw.stream_events(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        self.assertEqual(events[0].kind, "tool_calls")
        calls = events[0].tool_calls
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "call_1")
        self.assertEqual(calls[0].name, "get_weather")
        self.assertEqual(calls[0].arguments, {"location": "杭州"})
        self.assertEqual(events[1].kind, "done")
        self.assertEqual(events[1].finish_reason, "tool_calls")

    def test_multiple_tool_calls_by_index(self):
        svc = _StreamService([
            _tool(0, "a", "f1", '{"x":1}'),
            _tool(1, "b", "f2", '{}'),
            _finish(),
        ])
        gw = LLMGateway(service=svc)
        events = list(
            gw.stream_events(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        calls = events[0].tool_calls
        self.assertEqual([c.name for c in calls], ["f1", "f2"])
        self.assertEqual([c.id for c in calls], ["a", "b"])

    def test_bad_arguments_json_falls_back_empty(self):
        svc = _StreamService([_tool(0, "a", "f1", '{"broken'), _finish()])
        gw = LLMGateway(service=svc)
        events = list(
            gw.stream_events(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        self.assertEqual(events[0].tool_calls[0].arguments, {})

    def test_fallback_when_no_chat_stream(self):
        """底层无流式能力 → 单块完整文本 + done(与旧行为一致)。"""
        gw = LLMGateway(service=_PlainService())
        events = list(
            gw.stream_events(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        self.assertEqual(
            [(e.kind, e.delta) for e in events],
            [("text", "plain-full"), ("done", "")],
        )

    def test_tools_forwarded_to_service(self):
        seen = []
        svc = _StreamService([_finish()], tools_seen=seen)
        gw = LLMGateway(service=svc)
        tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
        list(gw.stream_events(LLMRequest(messages=[], tools=tools)))
        self.assertEqual(seen[0], tools)

    def test_complete_stream_only_text(self):
        """兼容接口:只透传文本分块,不产 tool_calls / done。"""
        svc = _StreamService([_text("a"), _tool(0, "x", "f", '{}'), _text("b"), _finish()])
        gw = LLMGateway(service=svc)
        chunks = list(
            gw.complete_stream(LLMRequest(messages=[{"role": "user", "content": "x"}]))
        )
        self.assertEqual([c.delta for c in chunks], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
