"""AI 服务工厂 —— 按名称装配服务实例，支持后续扩展。

当前默认仅注册 DeepSeek（见 runtime/llm/__init__.py）。
新增厂商 / 多模态服务：实现一个 AIService 子类，再在 __init__.py 中
_default_factory.register("name", ServiceClass) 即可，engine / 前端无需改动。
"""
from __future__ import annotations

import os

from .config_file import load_llm_config
from .errors import LLMError


class ServiceFactory:
    def __init__(self) -> None:
        self._registry: dict[str, type] = {}

    def register(self, name: str, cls: type) -> None:
        self._registry[name] = cls

    def create(self, name: str, **kwargs):
        if name not in self._registry:
            raise LLMError(
                f"未知 AI 服务：{name!r}。已注册：{sorted(self._registry)}"
            )
        return self._registry[name](**kwargs)

    def providers(self) -> list[str]:
        return sorted(self._registry)


# 进程级默认工厂（运行时单例）。
_default_factory = ServiceFactory()


def get_service(provider: str | None = None) -> "AIService":  # noqa: F821
    """取得默认（或指定名称的）服务实例。

    配置优先级（每个字段独立）：
        显式参数 > 环境变量 EIDOLON_LLM_<FIELD> > 配置文件 [llm] 段 > 服务内置默认
    provider 优先级：显式参数 > 环境变量 > 配置文件 > "deepseek"。
    """
    cfg = load_llm_config()
    provider = (
        provider
        or os.environ.get("EIDOLON_LLM_PROVIDER")
        or cfg.get("provider")
        or "deepseek"
    )
    if provider not in _default_factory._registry:
        raise LLMError(
            f"未知 AI 服务：{provider!r}。已注册：{sorted(_default_factory._registry)}"
        )
    kwargs: dict = {}
    for field in ("api_key", "base_url", "model", "temperature", "max_tokens"):
        val = os.environ.get("EIDOLON_LLM_" + field.upper()) or cfg.get(field)
        if val is not None:
            kwargs[field] = val
    return _default_factory._registry[provider](**kwargs)


def list_providers() -> list[str]:
    return _default_factory.providers()
