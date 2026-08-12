"""对话引擎:运行时组合层的会话核心。

重构后架构(两层抽象):
- ContextManager 管理上下文分层 + 编译 messages(不再手拼)
- LLMGateway 封装底层 LLM provider 差异(不再直接调 llm_chat)

engine 只负责:
1. 加载角色卡 → 设置 static 上下文
2. 接收用户输入 → 加入对话缓冲
3. 编译上下文 → 通过 gateway 发送 → 取回回复
4. 回复加入对话缓冲 → 返回

角色身份 = 模板(永久定义),对话历史 = 运行时状态,二者严格分离。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from eidolon_character_service import (
    load_character_file,
    CharacterLoadError,
    build_system_prompt,
    character_info as build_character_info,
)
from .llm import LLMUnconfigured, LLMError
from .llm_gateway import LLMGateway, LLMRequest
from .context import ContextManager

# 重新导出,保持原有 API 兼容
__all__ = ["RuntimeEngine", "ChatMessage", "CharacterLoadError"]


@dataclass
class ChatMessage:
    """对话消息(向后兼容的数据结构)。"""

    role: str  # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)


class RuntimeEngine:
    """一个运行时会话:持有一个已加载角色 + 一段对话历史。

    通过 LLMGateway + ContextManager 两层抽象与 LLM 交互,
    不直接操作 messages 拼接或 provider 选择。
    """

    # 上下文片段的语义标签(用于 ContextManager 内部标识)
    _TAG_CHARACTER_PROMPT = "character_prompt"

    def __init__(
        self,
        *,
        gateway: LLMGateway | None = None,
        context: ContextManager | None = None,
    ) -> None:
        self.character = None
        self.assets: dict[str, bytes] = {}
        self.asset_types: dict[str, Optional[str]] = {}
        self.manifest: dict = {}

        # 两层抽象:LLM 网关 + 上下文管理器
        self._gateway = gateway or LLMGateway()
        self._context = context or ContextManager()

        # 对话历史(向后兼容 history 属性)
        # 实际数据存储在 ContextManager 的 ConversationBuffer 中,
        # 此列表作为只读镜像供外部消费。
        self.history: list[ChatMessage] = []

    # ---- 加载 ----

    def load(self, path: str) -> dict:
        character, assets, manifest = load_character_file(path)
        self.character = character
        self.assets = assets
        self.manifest = manifest
        self.asset_types = {a.id: a.type for a in character.assets}

        # 将角色 system prompt 设置为静态层上下文
        system_prompt = build_system_prompt(character)
        self._context.set_static(self._TAG_CHARACTER_PROMPT, system_prompt)

        # 重置对话
        self._context.reset_conversation()
        self.history = []
        return self.character_info()

    # ---- 角色卡信息(供前端展示) ----

    def character_info(self) -> dict:
        if self.character is None:
            return {"loaded": False}
        return build_character_info(self.character)

    # ---- 对话历史 ----

    def reset(self) -> None:
        """清空对话历史(保留已加载角色和静态上下文)。"""
        self._context.reset_conversation()
        self.history = []

    def chat(self, user_message: str) -> dict:
        """处理一条用户消息,返回模型回复与对话历史。

        流程:
        1. 将用户消息加入上下文管理器(高频层)
        2. 编译上下文为 messages(缓存友好布局)
        3. 通过 LLM Gateway 发送请求
        4. 将回复加入上下文管理器
        5. 同步 history 镜像并返回
        """
        if self.character is None:
            raise CharacterLoadError("尚未加载角色卡,请先加载 .cart / .png。")

        # 1. 用户消息进入上下文
        self._context.add_message("user", user_message)
        self.history.append(ChatMessage(role="user", content=user_message))

        # 2. 编译上下文 → messages
        messages = self._context.compile()

        # 3. 通过 Gateway 发送
        request = LLMRequest(messages=messages)
        try:
            response = self._gateway.complete(request)
        except LLMUnconfigured:
            # 回滚:移除刚加入的用户消息(因为没有得到回复)
            self._context.reset_conversation()
            # 重新加入已有的历史(不含刚才那条)
            for m in self.history[:-1]:
                self._context.add_message(m.role, m.content)
            # 移除 history 末尾
            self.history.pop()
            raise
        except LLMError:
            # 同上回滚
            self._context.reset_conversation()
            for m in self.history[:-1]:
                self._context.add_message(m.role, m.content)
            self.history.pop()
            raise

        # 4. 回复进入上下文
        self._context.add_message("assistant", response.content)
        self.history.append(ChatMessage(role="assistant", content=response.content))

        return {
            "reply": response.content,
            "history": [
                {"role": m.role, "content": m.content} for m in self.history
            ],
        }

    # ---- 诊断信息 ----

    def context_cache_info(self) -> dict:
        """返回上下文缓存状态(供调试 / 性能优化)。"""
        return self._context.cache_info()

    def llm_provider(self) -> str:
        """当前使用的 LLM provider 名称。"""
        return self._gateway.provider
