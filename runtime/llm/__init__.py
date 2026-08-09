"""AI 服务层（工厂模式）。

默认仅注册 DeepSeek 文本对话服务；语音 / 视觉等多模态能力作为
AIService 基类上的扩展点预留，待实现对应服务后通过工厂注册即可。

对外暴露与旧版兼容的模块级 chat() 与异常，engine 无需改动。
"""
from __future__ import annotations

from .base import AIService
from .deepseek import DeepSeekService
from .errors import LLMError, LLMUnconfigured, UnsupportedCapability
from .factory import ServiceFactory, get_service, list_providers, _default_factory

# 注册默认服务：当前仅 deepseek。
# 扩展其他厂商 / 多模态服务时，在此追加一行 register 即可，engine / 前端零改动。
_default_factory.register("deepseek", DeepSeekService)

__all__ = [
    "ServiceFactory",
    "get_service",
    "list_providers",
    "AIService",
    "DeepSeekService",
    "LLMError",
    "LLMUnconfigured",
    "UnsupportedCapability",
    "chat",
]


def chat(messages: list[dict], *, stream: bool = False) -> str:
    """模块级兼容入口：用默认（工厂选出的）服务完成一次对话。"""
    return get_service().chat(messages, stream=stream)
