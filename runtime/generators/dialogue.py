"""角色对话生成器 —— 用户消息 → 角色回复。

从 RuntimeEngine.chat 迁入的调度职责:
1. 用户消息进入上下文(高频层)
2. 编译上下文为 messages(缓存友好布局)
3. 通过 LLM Gateway 发送
4. 回复进入上下文;LLM 未配置 / 出错时回滚本次用户消息
"""
from __future__ import annotations

from typing import Any, Optional

from ..context import ContextManager
from ..llm import LLMError, LLMUnconfigured
from ..llm_gateway import LLMGateway, LLMRequest
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
    ) -> None:
        super().__init__(gateway=gateway, context=context)
        self.character = character
        if system_prompt is not None:
            self._context.set_static(self._TAG_CHARACTER_PROMPT, system_prompt)

    def generate(self, payload: dict, *, stream: bool = False) -> dict:
        """输入 {"message": str} → 输出 {"reply": str, "history": [{role, content}]}。

        stream 为流式扩展位,本次未实现(传 True 抛 NotImplementedError)。
        """
        if stream:
            raise NotImplementedError("流式生成尚未实现")
        message = str((payload or {}).get("message", ""))
        if not message.strip():
            raise ValueError("message 不能为空")

        # 1. 用户消息进入上下文(高频层)
        self._context.add_message("user", message)
        history = self.history  # 含刚加入的用户消息

        # 2. 编译上下文 → messages
        messages = self._context.compile()

        # 3. 通过 Gateway 发送(失败回滚:移除刚加入的用户消息)
        request = LLMRequest(messages=messages)
        try:
            response = self._gateway.complete(request)
        except (LLMUnconfigured, LLMError):
            # 回滚:没有得到回复,重灌此前的历史(不含刚才那条)
            self._context.reset_conversation()
            for m in history[:-1]:
                self._context.add_message(m["role"], m["content"])
            raise

        # 4. 回复进入上下文
        self._context.add_message("assistant", response.content)
        return {"reply": response.content, "history": self.history}

    @property
    def history(self) -> list[dict]:
        """对话历史镜像 [{role, content}](最旧在前)。"""
        return [
            {"role": t.role, "content": t.content}
            for t in self._context.conversation_turns
        ]
