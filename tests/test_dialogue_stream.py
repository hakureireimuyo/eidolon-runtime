"""角色对话生成器流式路径测试(零网络,脚本化 mock 服务)。

覆盖:
- generate_stream:完整事件序列,chat.done 携带 history(含 segments)
- 同步 generate() 复用流式实现,两条路径结果一致
- 占位符解析后入史;注入注册表可注入(测试固定时间)
- LLM 出错 → chat.error + 上下文回滚(不留下未回复的 user 消息)
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from runtime.context import ContextManager
from runtime.generators.dialogue import DialogueGenerator
from runtime.inline import InterpreterRegistry
from runtime.llm.base import AIService, ProviderChunk
from runtime.llm.errors import LLMError, LLMUnconfigured
from runtime.llm_gateway import LLMGateway
from runtime.tools import ToolRegistry, ToolSpec


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


class _ScriptService(AIService):
    name = "script"

    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = 0

    def chat(self, messages, *, stream=False):
        return "x"

    def chat_stream(self, messages, *, tools=None):
        step = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        yield from step


class _FailingService(AIService):
    name = "failing"

    def chat(self, messages, *, stream=False):
        raise LLMError("boom")

    def chat_stream(self, messages, *, tools=None):
        raise LLMUnconfigured("未配置")

    def never(self):
        pass


def _character():
    return SimpleNamespace(identity=SimpleNamespace(name="TestBot"))


def _gen(service, **kw):
    char = kw.pop("character", _character())
    return DialogueGenerator(
        character=char,
        gateway=LLMGateway(service=service),
        context=kw.pop("context", ContextManager()),
        **kw,
    )


def _fixed_injections():
    reg = InterpreterRegistry()
    reg.register("time", lambda ctx: "12:00")
    return reg


class TestGenerateStream(unittest.TestCase):
    def test_full_event_sequence_with_tool_and_placeholder(self):
        rounds = [
            [_text("现在是⟦time⟧,我来开门"), _tool(0, "c1", "open_door", '{}'), _finish("tool_calls")],
            [_text("门开了"), _finish()],
        ]
        reg = ToolRegistry()
        reg.register(
            ToolSpec(
                name="open_door",
                description="开门",
                parameters={"type": "object", "properties": {}},
                label="开门",
                executor=lambda args: "门已打开",
            )
        )
        gen = _gen(_ScriptService(rounds), injections=_fixed_injections(), tool_registry=reg)
        events = list(gen.generate_stream({"message": "帮我开门"}))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, [
            "loop.turn", "text.delta", "text.delta", "text.delta",
            "tool.call", "tool.result",
            "loop.turn", "text.delta", "chat.done",
        ])
        # 占位符被解析替换(前端只见解析后内容)
        deltas = [e["delta"] for e in events if e["type"] == "text.delta"]
        self.assertEqual(deltas, ["现在是", "12:00", ",我来开门", "门开了"])
        done = events[-1]
        self.assertEqual(done["reply"]["text"], "现在是12:00,我来开门门开了")
        # history:user + assistant(segments 解析后带样式)
        self.assertEqual(len(done["history"]), 2)
        self.assertEqual(done["history"][0]["role"], "user")
        self.assertNotIn("segments", done["history"][0])
        self.assertEqual(done["history"][1]["role"], "assistant")
        self.assertIsNotNone(done["history"][1]["segments"])

    def test_sync_generate_matches_stream(self):
        """同步路径消费同一事件流,结果一致。"""
        rounds = [[_text("你好呀"), _finish()]]
        gen = _gen(_ScriptService(rounds))
        result = gen.generate({"message": "你好"})
        self.assertEqual(result["reply"], "你好呀")
        self.assertEqual(
            [m["role"] for m in result["history"]], ["user", "assistant"]
        )
        self.assertEqual(gen.context.conversation_turns[-1].content, "你好呀")

    def test_llm_error_rollback_with_event(self):
        gen = _gen(_FailingService())
        # 先成功一轮(用正常上下文)
        events = list(gen.generate_stream({"message": "触发失败"}))
        err = events[-1]
        self.assertEqual(err["type"], "chat.error")
        self.assertEqual(err["code"], "unconfigured")
        # 回滚:历史为空(刚加入的 user 消息被移除)
        self.assertEqual(len(gen.context.conversation_turns), 0)

    def test_llm_error_keeps_prior_turns(self):
        """失败回滚只移除本轮 user 消息,保留此前历史。"""
        gen = _gen(_ScriptService([[_text("第一条"), _finish()]]))
        gen.generate({"message": "先聊一句"})
        gen._gateway = LLMGateway(service=_FailingService())
        events = list(gen.generate_stream({"message": "触发失败"}))
        self.assertEqual(events[-1]["type"], "chat.error")
        turns = gen.context.conversation_turns
        self.assertEqual([t.role for t in turns], ["user", "assistant"])
        self.assertEqual(turns[1].content, "第一条")


if __name__ == "__main__":
    unittest.main()
