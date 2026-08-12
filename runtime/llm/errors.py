"""AI 服务层异常。"""
from __future__ import annotations


class LLMError(Exception):
    """AI 服务层通用错误(网络 / 远端拒绝 / 依赖缺失等)。"""


class LLMUnconfigured(LLMError):
    """未配置可用的 API Key,无法发起真实对话。"""


class UnsupportedCapability(LLMError):
    """该服务未实现某多模态能力——这是「预留扩展点」,待对应服务实现。

    例如默认 DeepSeek 文本服务不支持语音/视觉,调用其
    transcribe_audio / synthesize_speech / describe_image 会抛此异常。
    """

    def __init__(self, capability: str, provider: str):
        self.capability = capability
        self.provider = provider
        super().__init__(
            f"AI 服务 {provider!r} 暂不支持多模态能力 {capability!r}"
            f"(已预留扩展点,请实现对应服务后通过工厂注册)。"
        )
