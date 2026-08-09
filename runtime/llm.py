"""AI 模型调用层：OpenAI 兼容客户端。

兼容任意 OpenAI 协议端点（OpenAI / DeepSeek / 通义千问 / 硅基流动 / Ollama 等），
只需通过环境变量切换 base_url 与 model。未配置 API Key 时优雅降级，
让「加载角色卡」仍可离线完成，「对话」在配置后自动可用。
"""
from __future__ import annotations

from .config import (
    LLM_BASE_URL,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    llm_configured,
)


class LLMUnconfigured(Exception):
    """未配置 LLM（缺 API Key）。"""


class LLMError(Exception):
    """调用 LLM 过程中的错误（依赖缺失 / 网络 / 远端拒绝等）。"""


def chat(messages: list[dict], *, stream: bool = False) -> str:
    """发起一次对话补全，返回模型文本回复。

    messages: OpenAI 风格消息列表，如
        [{"role": "system", ...}, {"role": "user", ...}, ...]
    """
    if not llm_configured():
        raise LLMUnconfigured(
            "未配置 LLM API Key。请设置环境变量 EIDOLON_LLM_API_KEY"
            "（以及可选的 EIDOLON_LLM_BASE_URL / EIDOLON_LLM_MODEL）。"
        )
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover
        raise LLMError("缺少 openai 依赖，请先执行 pip install openai。") from exc

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            stream=False,
        )
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"调用 LLM 失败：{exc}") from exc

    return resp.choices[0].message.content or ""
