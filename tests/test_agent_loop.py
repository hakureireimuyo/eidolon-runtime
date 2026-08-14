"""AgentLoop 事件内循环测试(零网络,脚本化 mock 服务)。

覆盖(对齐 docs/streaming-event-loop-placeholder.md §5):
- 单轮文本 → done;解析后文本入史(含 segments)
- 工具调用循环:tool 消息为循环局部 transient,不入用户可见历史;
  执行结果回流 → 下一轮 LLM
- 工具失败 → 错误文本回流,模型自行挽救(世界不回滚)
- max_turns 耗尽 → force_final 无工具收尾
- 占位符在流中被解析替换后入史
- cancel_check → LoopCancelled 在检查点退出
"""
from __future__ import annotations

import unittest

from runtime.agent_loop import AgentLoop, LoopCancelled
from runtime.context import ContextManager
from runtime.inline import InterpreterRegistry
from runtime.llm.base import AIService, ProviderChunk
from runtime.llm_gateway import LLMGateway
from runtime.tools import ToolRegistry, ToolSpec


def _text(s: str) -> ProviderChunk:
    return ProviderChunk(kind="text", delta=s)


def _tool(idx: int, call_id: str, name: str, args: str) -> ProviderChunk:
    return ProviderChunk(
        kind="tool_call",
        delta=args,
        tool_call_index=idx,
        tool_call_id=call_id,
        tool_call_name=name,
    )


def _finish(reason: str = "stop") -> ProviderChunk:
    return ProviderChunk(kind="finish", finish_reason=reason)


class _ScriptService(AIService):
    """脚本化流式服务:按轮次回放预定分块,记录每轮 messages / tools。"""

    name = "script"

    def __init__(self, rounds):
        self.rounds = rounds  # list[list[ProviderChunk]]
        self.calls: list[list[dict]] = []   # 每轮 messages 快照
        self.tools_seen: list = []          # 每轮 tools 参数

    def chat(self, messages, *, stream=False):
        return "[sync]"

    def chat_stream(self, messages, *, tools=None):
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        step = self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]
        yield from step


def _door_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        ToolSpec(
            name="open_door",
            description="尝试开门",
            parameters={"type": "object", "properties": {}},
            label="开门",
            executor=lambda args: "门已打开",
        )
    )
    return reg


def _events_of(loop: AgentLoop) -> list[dict]:
    return list(loop.run())


class TestSingleTurn(unittest.TestCase):
    def test_text_round_done(self):
        ctx = ContextManager()
        ctx.add_message("user", "你好")
        svc = _ScriptService([[_text("你好呀"), _finish()]])
        loop = AgentLoop(gateway=LLMGateway(service=svc), context=ctx)
        events = _events_of(loop)
        kinds = [e["type"] for e in events]
        self.assertEqual(
            kinds, ["loop.turn", "text.delta", "chat.done"]
        )
        self.assertEqual(events[0]["turn"], 1)
        self.assertEqual(events[0]["reason"], "start")
        self.assertEqual(events[1]["delta"], "你好呀")
        self.assertEqual(events[1]["style"], "plain")
        done = events[-1]
        self.assertEqual(done["reply"]["text"], "你好呀")
        self.assertEqual(done["reply"]["segments"], [{"text": "你好呀", "style": "plain"}])
        # 入史:user + assistant(解析后文本)
        turns = ctx.conversation_turns
        self.assertEqual([t.role for t in turns], ["user", "assistant"])
        self.assertEqual(turns[1].content, "你好呀")
        self.assertIsNotNone(turns[1].segments)
        # 无工具声明时 tools=None
        self.assertIsNone(svc.tools_seen[0])

    def test_placeholder_resolved_before_history(self):
        ctx = ContextManager()
        ctx.add_message("user", "几点了")
        svc = _ScriptService([[_text("现在⟦time⟧了"), _finish()]])
        reg = InterpreterRegistry()
        reg.register("time", lambda c: "12:00")
        loop = AgentLoop(
            gateway=LLMGateway(service=svc), context=ctx,
            resolver=reg.resolve, ctx=None,
        )
        events = _events_of(loop)
        deltas = [e["delta"] for e in events if e["type"] == "text.delta"]
        self.assertEqual(deltas, ["现在", "12:00", "了"])
        # 入史的是解析后固定文本,原文已丢弃
        self.assertEqual(ctx.conversation_turns[-1].content, "现在12:00了")
        self.assertEqual(events[-1]["reply"]["text"], "现在12:00了")

    def test_unregistered_injection_silent(self):
        ctx = ContextManager()
        ctx.add_message("user", "hi")
        svc = _ScriptService([[_text("⟦ghost⟧好"), _finish()]])
        loop = AgentLoop(gateway=LLMGateway(service=svc), context=ctx)
        events = _events_of(loop)
        deltas = [e["delta"] for e in events if e["type"] == "text.delta"]
        self.assertEqual(deltas, ["好"])
        self.assertEqual(ctx.conversation_turns[-1].content, "好")


class TestToolLoop(unittest.TestCase):
    def _setup(self, rounds, registry=None, max_turns=8):
        ctx = ContextManager()
        ctx.add_message("user", "帮我开门")
        svc = _ScriptService(rounds)
        loop = AgentLoop(
            gateway=LLMGateway(service=svc),
            context=ctx,
            registry=registry if registry is not None else _door_registry(),
            max_turns=max_turns,
        )
        return ctx, svc, loop

    def test_two_turn_tool_loop(self):
        ctx, svc, loop = self._setup([
            [_text("我来开门"), _tool(0, "c1", "open_door", '{}'), _finish("tool_calls")],
            [_text("门开了"), _finish()],
        ])
        events = _events_of(loop)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, [
            "loop.turn", "text.delta", "tool.call", "tool.result",
            "loop.turn", "text.delta", "chat.done",
        ])
        self.assertEqual(events[2]["name"], "open_door")
        self.assertEqual(events[2]["label"], "开门")
        self.assertTrue(events[3]["ok"])
        self.assertEqual(events[4]["turn"], 2)
        self.assertEqual(events[4]["reason"], "tool_result")
        # 最终文本 = 两轮文本拼接
        self.assertEqual(events[-1]["reply"]["text"], "我来开门门开了")
        # 用户可见历史不含工具痕迹
        self.assertEqual(
            [t.role for t in ctx.conversation_turns], ["user", "assistant"]
        )
        # 第二轮 messages 尾部 = assistant(tool_calls) + tool 结果
        tail = svc.calls[1][-2:]
        self.assertEqual(tail[0]["role"], "assistant")
        self.assertEqual(tail[0]["tool_calls"][0]["function"]["name"], "open_door")
        self.assertEqual(tail[1]["role"], "tool")
        self.assertEqual(tail[1]["content"], "门已打开")
        # 第二轮仍带工具声明
        self.assertEqual(svc.tools_seen[1][0]["function"]["name"], "open_door")

    def test_tool_error_flows_back(self):
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="open_door",
                description="尝试开门",
                parameters={"type": "object", "properties": {}},
                executor=lambda args: (_ for _ in ()).throw(RuntimeError("卡住了")),
            )
        )
        ctx, svc, loop = self._setup([
            [_tool(0, "c1", "open_door", '{}'), _finish("tool_calls")],
            [_text("打不开,算了吧"), _finish()],
        ], registry=reg)
        events = _events_of(loop)
        result_ev = next(e for e in events if e["type"] == "tool.result")
        self.assertFalse(result_ev["ok"])
        self.assertIn("卡住了", result_ev["error"])
        # 错误文本回流模型(transient),循环继续
        self.assertIn("执行失败", svc.calls[1][-1]["content"])
        self.assertEqual(events[-1]["reply"]["text"], "打不开,算了吧")

    def test_unregistered_tool_error(self):
        ctx, svc, loop = self._setup([
            [_tool(0, "c1", "ghost_tool", '{}'), _finish("tool_calls")],
            [_text("抱歉"), _finish()],
        ])
        events = _events_of(loop)
        result_ev = next(e for e in events if e["type"] == "tool.result")
        self.assertFalse(result_ev["ok"])
        self.assertIn("未注册工具", result_ev["error"])
        self.assertEqual(events[-1]["reply"]["text"], "抱歉")

    def test_no_registry_no_tools_declared(self):
        """工具系统未接入:不声明工具,纯文本单轮正常。"""
        ctx, svc, loop = self._setup([
            [_text("好的"), _finish()],
        ], registry=None)
        events = _events_of(loop)
        self.assertIsNone(svc.tools_seen[0])
        self.assertEqual(events[-1]["reply"]["text"], "好的")

    def test_max_turns_force_final(self):
        rounds = [
            [_tool(0, f"c{i}", "open_door", '{}'), _finish("tool_calls")]
            for i in range(6)
        ]
        rounds.append([_text("最终回复"), _finish()])
        ctx, svc, loop = self._setup(rounds, max_turns=3)
        events = _events_of(loop)
        kinds = [e["type"] for e in events]
        # 3 轮工具循环 + 1 轮 force_final
        self.assertEqual(kinds.count("loop.turn"), 4)
        final_turn = next(e for e in events if e["type"] == "loop.turn" and e["turn"] == 4)
        self.assertEqual(final_turn["reason"], "continue")
        # force_final 轮:无工具声明,尾部追加收尾指令
        self.assertIsNone(svc.tools_seen[3])
        self.assertIn("不要再调用任何工具", svc.calls[3][-1]["content"])
        self.assertEqual(events[-1]["reply"]["text"], "最终回复")
        self.assertEqual(
            [t.role for t in ctx.conversation_turns], ["user", "assistant"]
        )

    def test_cancel_at_checkpoint(self):
        ctx, svc, loop = self._setup([[_text("hi"), _finish()]])
        loop._cancel_check = lambda: True  # 测试:首轮即取消
        with self.assertRaises(LoopCancelled):
            _events_of(loop)
        # 循环未提交任何入史内容(用户消息仍在,由调用方回滚)
        self.assertEqual([t.role for t in ctx.conversation_turns], ["user"])


if __name__ == "__main__":
    unittest.main()
