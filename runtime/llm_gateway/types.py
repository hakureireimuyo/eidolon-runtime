"""LLM Gateway 类型定义 —— 请求与响应的抽象数据结构。

设计原则：
- engine / 未来 agent 只构造 LLMRequest、消费 LLMResponse，
  不感知底层 provider（DeepSeek / OpenAI / 本地模型等）的任何细节。
- 参数字段（temperature / max_tokens / stream）在 request 上声明，
  由 Gateway 决定如何传递给底层服务——如果底层暂不支持 per-request 覆写，
  则使用服务自身配置，不报错（优雅降级）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class LLMRequest:
    """对 LLM 的一次补全请求。

    messages: OpenAI 风格消息列表（由 ContextManager 编译产出）。
    temperature: 可选，覆盖服务默认温度。
    max_tokens: 可选，覆盖服务默认最大输出长度。
    stream: 是否流式输出（当前默认 False）。
    """

    messages: list[dict]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False

    def with_messages(self, messages: list[dict]) -> "LLMRequest":
        """返回一个替换了 messages 的新请求（不可变风格，便于链式调用）。"""
        return LLMRequest(
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=self.stream,
        )


@dataclass
class LLMResponse:
    """LLM 补全的结构化响应。

    content: 模型文本回复。
    provider: 提供本次服务的底层 provider 名称（如 "deepseek"）。
    finish_reason: 结束原因（"stop" / "length" / ...），当前可能为 None。
    usage: token 用量统计（{prompt_tokens, completion_tokens, total_tokens}），
           如果底层服务不返回则为空 dict。
    """

    content: str
    provider: str = ""
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.content


@dataclass
class LLMStreamChunk:
    """流式输出的单个分块（未来扩展用）。"""

    delta: str
    finish_reason: str | None = None

    def __iter__(self) -> Iterator[str]:
        yield self.delta
