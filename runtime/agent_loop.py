"""AgentLoop —— agent 事件内循环(LLM ↔ 工具的调度核心)。

一次 chat = 一个内循环;每轮 LLM 输出先化为事件,再决定下一步:

- 文本增量 → StreamParser(⟦⟧ 协议解析)→ text.delta 事件;
- tool_call → ToolRegistry 执行 → tool.call / tool.result 事件 →
  tool 消息回流(循环局部 transient,尾部拼接,**不入用户可见历史**);
- 无工具调用 → 解析后文本一次性入史(入史即固定,永不重解析)→ chat.done。

终止条件:
- 本轮无 tool_call → 正常结束;
- max_turns 耗尽 → force_final 强制一次无工具收尾调用;
- 断连(生成器 close / cancel_check)→ LoopCancelled,循环在检查点退出,
  已执行工具的世界效果不回滚(程序真实性),回滚只作用于上下文。

对齐 docs/streaming-event-loop-placeholder.md §5。
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterator, Optional

from .context import ContextManager
from .inline import StreamParser
from .llm_gateway import LLMGateway, LLMRequest, ToolCall
from .tools import ToolError, ToolRegistry

#: 内循环默认轮次上限。
DEFAULT_MAX_TURNS = 8

#: force_final 收尾指令(追加在 transient 尾部,不入历史)。
FINAL_INSTRUCTION = "请直接以自然语言回复,不要再调用任何工具。"


class LoopCancelled(Exception):
    """循环被取消(断连 / 显式取消);世界效果不回滚,上下文由调用方回滚。"""


class AgentLoop:
    """事件内循环:消费 LLM 事件流,编排工具执行,产出协议事件。

    事件类型(对齐规范 §3.1):
      loop.turn / text.delta / tool.call / tool.result / chat.done / chat.error

    - resolver: 占位符解析应答(InterpreterRegistry.resolve);None = 程序
      未接入,一切注入静默替换为空;
    - registry / tool_names: 工具注册表与允许集(None = 全部 / 未接入);
    - cancel_check: 每个检查点(每轮开始、force_final 前)询问是否取消。
    """

    def __init__(
        self,
        *,
        gateway: LLMGateway,
        context: ContextManager,
        registry: ToolRegistry | None = None,
        tool_names: list[str] | None = None,
        resolver: Optional[Callable[[str, Any], Optional[str]]] = None,
        ctx: Any = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        force_final: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._gateway = gateway
        self._context = context
        self._registry = registry
        self._tool_names = tool_names
        self._resolver = resolver
        self._ctx = ctx
        self._max_turns = max_turns
        self._force_final = force_final
        self._cancel_check = cancel_check

    def run(self) -> Iterator[dict]:
        """运行内循环,产出协议事件(生成器;close() 即取消)。"""
        parser = StreamParser(resolver=self._resolver, ctx=self._ctx)
        transient: list[dict] = []
        turn = 0
        reason = "start"
        while turn < self._max_turns:
            turn += 1
            self._check_cancel()
            yield {"type": "loop.turn", "turn": turn, "reason": reason}
            events, calls = self._round(parser, transient, use_tools=True)
            yield from events
            if not calls:
                yield self._commit(parser)  # 入史 + done
                return
            # 工具执行:调用与结果回流 transient(循环局部,不入历史)
            transient.append(self._tool_calls_message(calls))
            for call in calls:
                yield from self._execute_tool(call, transient)
            reason = "tool_result"

        # max_turns 耗尽:force_final 强制无工具收尾,保证用户拿到成段文本
        if self._force_final:
            self._check_cancel()
            turn += 1
            yield {"type": "loop.turn", "turn": turn, "reason": "continue"}
            events, calls = self._round(
                parser,
                transient + [{"role": "user", "content": FINAL_INSTRUCTION}],
                use_tools=False,
            )
            yield from events
            yield self._commit(parser)  # 入史 + done
            return
        yield {
            "type": "chat.error",
            "code": "max_turns",
            "message": "生成超时:内循环轮次耗尽且无文本输出",
        }

    # ---- 内部 ----

    def _round(
        self, parser: StreamParser, transient: list[dict], *, use_tools: bool
    ) -> tuple[list[dict], list[ToolCall]]:
        """一轮 LLM 调用:transient 尾部拼接,产出解析后事件与工具调用。"""
        messages = self._context.compile(transient=transient)
        tools = None
        if use_tools and self._registry is not None:
            tools = self._registry.to_openai(self._tool_names)
        events: list[dict] = []
        calls: list[ToolCall] = []
        for ev in self._gateway.stream_events(LLMRequest(messages=messages, tools=tools)):
            if ev.kind == "text":
                for ie in parser.feed(ev.delta):
                    events.append(
                        {"type": "text.delta", "delta": ie.delta, "style": ie.style}
                    )
            elif ev.kind == "tool_calls":
                calls = ev.tool_calls
        parser.finish()  # 轮边界:未闭合占位符静默丢弃
        return events, calls

    def _execute_tool(self, call: ToolCall, transient: list[dict]) -> Iterator[dict]:
        """执行一个工具调用:事件发出 + 结果文本回流 transient。"""
        spec = self._registry.get(call.name) if self._registry is not None else None
        yield {
            "type": "tool.call",
            "id": call.id,
            "name": call.name,
            "label": (spec.label if spec else ""),
            "args": call.arguments,
        }
        try:
            if self._registry is None:
                raise ToolError("工具系统未接入")
            result = self._registry.execute(call.name, call.arguments)
            ok, error = True, None
        except ToolError as exc:
            # 错误文本回流模型,由模型自行挽救;世界不回滚
            result, ok, error = str(exc), False, str(exc)
        yield {
            "type": "tool.result",
            "id": call.id,
            "name": call.name,
            "ok": ok,
            "error": error,
        }
        transient.append(
            {"role": "tool", "tool_call_id": call.id, "content": result}
        )

    def _commit(self, parser: StreamParser) -> dict:
        """最终文本入史:解析后纯文本(入史即固定,永不重解析)。

        返回 chat.done(含 segments,供前端最终同步);无文本则 chat.error。
        """
        text = parser.resolved_text
        if text.strip():
            self._context.add_message(
                "assistant", text, segments=parser.segments
            )
            return {
                "type": "chat.done",
                "reply": {"text": text, "segments": parser.segments},
            }
        return {
            "type": "chat.error",
            "code": "empty_reply",
            "message": "生成失败:内循环结束但没有文本输出",
        }

    @staticmethod
    def _tool_calls_message(calls: list[ToolCall]) -> dict:
        """工具调用转 OpenAI 风格 assistant 消息(循环局部)。"""
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments, ensure_ascii=False),
                    },
                }
                for c in calls
            ],
        }

    def _check_cancel(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise LoopCancelled("生成已取消")
