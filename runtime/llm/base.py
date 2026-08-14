"""AI 服务抽象基类 —— 统一契约,便于工厂装配与多模态扩展。

设计原则(对齐 PersonaSeed 架构约定):
- 协议层只定义「契约」,不绑定任何厂商；
- 多模态能力(语音 / 视觉等)作为**显式扩展点**声明在基类,
  默认实现直接抛出 UnsupportedCapability,绝不提供假实现(不做临时方案)；
- messages 的 content 允许是 OpenAI 风格的多模态内容块
  ([{type:"text",text:...}, {type:"image_url",image_url:{url:...}}]),
  因此数据模型本就支持视觉,未来视觉服务只需覆写对应方法。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

from .errors import UnsupportedCapability


@dataclass
class ProviderChunk:
    """流式输出的底层分块(provider 无关形态)。

    kind:
      "text"      文本增量(delta)
      "tool_call" 工具调用片段(同 tool_call_index 组装;首个片段携带 id / name)
      "finish"    流结束(finish_reason)
    思考模式内容(reasoning_content)由服务层丢弃,不进入此契约。
    """

    kind: str = "text"
    delta: str = ""
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    finish_reason: str | None = None


class AIService(ABC):
    """一个 AI 服务(厂商 / 能力组合)。"""

    #: 服务名(与工厂注册键一致)。
    name: str = "base"

    # ---- 文本对话(核心能力,每个服务必须实现) ----
    @abstractmethod
    def chat(self, messages: list[dict], *, stream: bool = False) -> str:
        """发起一次对话补全,返回模型文本回复。

        messages 为 OpenAI 风格消息列表；content 可为字符串,
        亦可为多模态内容块列表(文本 + 图片),由具体服务决定是否消费。
        """

    def chat_stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> Iterator[ProviderChunk]:
        """流式对话补全(扩展点,默认不支持)。

        产出 ProviderChunk 序列(文本增量 / 工具调用片段 / 结束);
        tools 为 OpenAI 风格工具声明。未实现流式的服务由 LLMGateway
        优雅降级为单块返回。
        """
        raise UnsupportedCapability("chat_stream", self.name)

    # ---- 多模态能力(扩展点,默认不支持) ----
    def transcribe_audio(self, audio: bytes, *, mime: str | None = None) -> str:
        """语音 -> 文本。未来语音服务覆写此方法。"""
        raise UnsupportedCapability("transcribe_audio", self.name)

    def synthesize_speech(self, text: str) -> bytes:
        """文本 -> 语音。未来 TTS 服务覆写此方法。"""
        raise UnsupportedCapability("synthesize_speech", self.name)

    def describe_image(self, image: bytes, *, prompt: str = "", mime: str | None = None) -> str:
        """视觉理解:看图说话。未来视觉服务覆写此方法。"""
        raise UnsupportedCapability("describe_image", self.name)

    # ---- 能力声明(供上层按需启用对应 UI) ----
    @property
    def capabilities(self) -> set[str]:
        """该服务当前支持的能力名集合。"""
        return {"chat"}
