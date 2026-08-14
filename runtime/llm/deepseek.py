"""DeepSeek 服务(默认且仅注册的 AI 服务,V1)。

DeepSeek 提供 OpenAI 兼容接口,因此直接复用 openai SDK。
服务只实现文本对话(chat)；多模态能力沿用基类默认(抛 UnsupportedCapability)。
由于 messages 原样转发给接口,若日后切换到支持视觉的模型,多模态内容块可直通。
"""
from __future__ import annotations

import os
from typing import Iterator

from .base import AIService, ProviderChunk
from .errors import LLMError, LLMUnconfigured


class DeepSeekService(AIService):
    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        # 兼容旧版通用变量:EIDOLON_LLM_API_KEY 作为 Key 兜底。
        self.api_key = (
            api_key
            or os.environ.get("EIDOLON_DEEPSEEK_API_KEY")
            or os.environ.get("EIDOLON_LLM_API_KEY", "")
        )
        self.base_url = base_url or os.environ.get(
            "EIDOLON_DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
        self.model = model or os.environ.get("EIDOLON_DEEPSEEK_MODEL", "deepseek-chat")
        # 显式传入的 temperature / max_tokens(来自 env 或 config.toml)优先于默认值。
        self.temperature = (
            float(temperature)
            if temperature is not None
            else float(os.environ.get("EIDOLON_LLM_TEMPERATURE", "0.8"))
        )
        self.max_tokens = (
            int(max_tokens)
            if max_tokens is not None
            else int(os.environ.get("EIDOLON_LLM_MAX_TOKENS", "1024"))
        )

    def chat(self, messages: list[dict], *, stream: bool = False) -> str:
        if not self.api_key:
            raise LLMUnconfigured(
                "未配置 DeepSeek API Key。请设置 EIDOLON_DEEPSEEK_API_KEY"
                "(或通用兜底 EIDOLON_LLM_API_KEY)。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少 openai 依赖,请先执行 pip install openai。") from exc

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"调用 DeepSeek 失败:{exc}") from exc

        return resp.choices[0].message.content or ""

    def chat_stream(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
    ) -> Iterator[ProviderChunk]:
        """流式对话:产出文本 / 工具调用片段分块。

        思考模式内容(reasoning_content)不进入渲染,按配置丢弃;
        工具调用片段按 tool_call_index 组装(组装在 Gateway 层完成)。
        """
        if not self.api_key:
            raise LLMUnconfigured(
                "未配置 DeepSeek API Key。请设置 EIDOLON_DEEPSEEK_API_KEY"
                "(或通用兜底 EIDOLON_LLM_API_KEY)。"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError("缺少 openai 依赖,请先执行 pip install openai。") from exc

        client = OpenAI(base_url=self.base_url, api_key=self.api_key)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
                tools=tools or None,
            )
            for chunk in resp:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                # 思考模式内容不进入渲染(按配置丢弃)
                if getattr(delta, "reasoning_content", None):
                    continue
                content = delta.content
                if content:
                    yield ProviderChunk(kind="text", delta=content)
                for tc in delta.tool_calls or []:
                    fn = tc.function
                    yield ProviderChunk(
                        kind="tool_call",
                        delta=(fn.arguments if fn and fn.arguments else ""),
                        tool_call_index=tc.index if tc.index is not None else 0,
                        tool_call_id=tc.id,
                        tool_call_name=(fn.name if fn and fn.name else None),
                    )
                if choice.finish_reason:
                    yield ProviderChunk(
                        kind="finish", finish_reason=choice.finish_reason
                    )
        except (LLMUnconfigured, LLMError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"调用 DeepSeek 失败:{exc}") from exc
