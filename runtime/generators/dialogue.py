"""角色对话生成器 —— 用户消息 → 角色回复。

调度职责(对齐 docs/streaming-event-loop-placeholder.md):
1. 用户消息进入上下文(高频层)
2. AgentLoop 事件内循环:LLM ↔ 工具,流式文本经内联协议解析层
   (⟦⟧ 占位符)解析后才渲染 / 入史
3. 解析后文本一次性入史(入史即固定,永不重解析;原文丢弃)
4. LLM 未配置 / 出错时回滚本次用户消息(与旧行为一致)

同步 generate() 复用流式实现(消费完整事件流),保证两条路径
行为一致;注入应答(InterpreterRegistry)与工具( ToolRegistry)为
可注入扩展点——未接入即静默替换 / 未注册工具错误回流模型。
"""
from __future__ import annotations

import time
from typing import Any, Iterator, Optional

from ..agent_loop import AgentLoop, LoopCancelled
from ..context import ContextManager
from ..inline import InterpreterRegistry
from ..llm import LLMError, LLMUnconfigured
from ..llm_gateway import LLMGateway
from ..tools import ToolRegistry
from .base import Generator


class DialogueGenerator(Generator):
    """角色对话生成器:自管理对话上下文(角色设定 static 段 + 对话历史)。"""

    id = "dialogue"
    label = "角色对话"

    # 上下文片段的语义标签(角色 system prompt 所在段)
    _TAG_CHARACTER_PROMPT = "character_prompt"

    def __init__(
        self,
        character: Any,
        *,
        gateway: LLMGateway | None = None,
        context: ContextManager | None = None,
        system_prompt: Optional[str] = None,
        injections: InterpreterRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_names: Optional[list[str]] = None,
    ) -> None:
        super().__init__(gateway=gateway, context=context)
        self.character = character
        if system_prompt is not None:
            self._context.set_static(self._TAG_CHARACTER_PROMPT, system_prompt)
        # 注入解析注册表(程序接入点):缺省接入 time / char:name,
        # 其余路径未接入 → 解析层静默替换为空。
        self._injections = injections or self._default_injections()
        # 工具注册表:V1 缺省未接入任何工具(协议与循环先行)
        self._tool_registry = tool_registry
        self._tool_names = tool_names

    def _default_injections(self) -> InterpreterRegistry:
        registry = InterpreterRegistry()
        registry.register("time", lambda ctx: time.strftime("%H:%M"))
        registry.register(
            "char:name",
            lambda ctx: (
                getattr(getattr(ctx, "identity", None), "name", None)
                if ctx is not None
                else None
            ),
        )
        return registry

    # ---- 同步生成(复用流式实现) ----

    def generate(self, payload: dict, *, stream: bool = False) -> dict:
        """输入 {"message": str} → 输出 {"reply": str, "history": [...]}。

        stream 为流式扩展位,本次未实现(传 True 抛 NotImplementedError)。
        """
        if stream:
            raise NotImplementedError("流式生成请使用 generate_stream()")
        events = list(self.generate_stream(payload))
        done = next((e for e in events if e["type"] == "chat.done"), None)
        err = next((e for e in events if e["type"] == "chat.error"), None)
        if err is not None:
            if err["code"] == "unconfigured":
                raise LLMUnconfigured(err["message"])
            raise LLMError(err["message"])
        assert done is not None  # 无错误必有 done
        return {"reply": done["reply"]["text"], "history": done["history"]}

    # ---- 流式生成 ----

    def generate_stream(self, payload: dict) -> Iterator[dict]:
        """输入 {"message": str} → 产出协议事件 dict(见 AgentLoop)。"""
        message = str((payload or {}).get("message", ""))
        if not message.strip():
            raise ValueError("message 不能为空")

        # 1. 用户消息进入上下文(高频层);快照用于失败回滚
        self._context.add_message("user", message)
        prior = self._context.conversation_turns[:-1]

        # 2. AgentLoop:LLM ↔ 工具内循环 + 占位符解析
        loop = AgentLoop(
            gateway=self._gateway,
            context=self._context,
            registry=self._tool_registry,
            tool_names=self._tool_names,
            resolver=self._injections.resolve,
            ctx=self.character,
        )
        try:
            for ev in loop.run():
                if ev["type"] == "chat.done":
                    ev["history"] = self.history
                yield ev
        except (LLMUnconfigured, LLMError) as exc:
            # 回滚:没有得到回复,重灌此前历史(不含刚才那条用户消息)
            self._restore(prior)
            code = (
                "unconfigured"
                if isinstance(exc, LLMUnconfigured)
                else "llm_error"
            )
            yield {"type": "chat.error", "code": code, "message": str(exc)}
        except LoopCancelled as exc:
            # 取消:上下文回滚,世界效果不回滚(程序真实性)
            self._restore(prior)
            yield {"type": "chat.error", "code": "cancelled", "message": str(exc)}

    def _restore(self, prior: list) -> None:
        """回滚对话缓冲区到快照状态。"""
        self._context.reset_conversation()
        for t in prior:
            self._context.add_message(t.role, t.content, t.segments)

    @property
    def history(self) -> list[dict]:
        """对话历史镜像 [{role, content, segments?}](最旧在前)。"""
        out: list[dict] = []
        for t in self._context.conversation_turns:
            item: dict = {"role": t.role, "content": t.content}
            if t.segments is not None:
                item["segments"] = t.segments
            out.append(item)
        return out
