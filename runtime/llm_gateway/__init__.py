"""LLM Gateway —— LLM 抽象层。

位于 LLM 服务层(runtime/llm/)之上、具体调度之下。
封装底层 provider 差异,提供结构化 I/O(LLMRequest → LLMResponse)。

eidolon-runtime 通过 LLMGateway 与 LLM 交互,不直接操作 AIService。
"""
from __future__ import annotations

from .gateway import LLMGateway
from .types import (
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMStreamEvent,
    ToolCall,
)

__all__ = [
    "LLMGateway",
    "LLMRequest",
    "LLMResponse",
    "LLMStreamChunk",
    "LLMStreamEvent",
    "ToolCall",
]
