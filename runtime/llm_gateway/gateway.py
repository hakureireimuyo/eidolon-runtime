"""LLM Gateway —— LLM 抽象层。

这一层位于 **LLM 服务层（runtime/llm/）之上、具体调度之下**。
它的核心职责：

1. **封装 provider 差异**：engine 只需调用 `gateway.complete(request)`，
   不需要知道当前是 DeepSeek、OpenAI 还是本地模型在提供服务。
2. **结构化 I/O**：输入 LLMRequest（messages + 可选参数），
   输出 LLMResponse（content + 元数据），而非裸 list[dict] / str。
3. **多 agent 就绪**：多个 agent 共享同一 Gateway 实例，
   各自构造 LLMRequest 即可，Gateway 不持有任何会话状态。
4. **参数管理**：per-request 参数（temperature / max_tokens）在 request 上声明，
   Gateway 尝试传递到底层；若底层服务不支持 per-request 覆写，则优雅降级
   为服务自身配置，不报错。

不负责的：
- 上下文编译（ContextManager 职责）
- 状态管理（各能力子项目职责）
- 会话维护（ContextManager / engine 职责）
"""
from __future__ import annotations

from typing import Iterator

from ..llm.base import AIService
from ..llm.factory import get_service, _default_factory
from ..llm.config_file import load_llm_config
from ..llm.errors import LLMError, LLMUnconfigured
from .types import LLMRequest, LLMResponse, LLMStreamChunk


class LLMGateway:
    """LLM 抽象网关 —— eidolon-runtime 与 LLM 交互的统一入口。

    使用方式：
        gateway = LLMGateway()           # 使用默认工厂配置
        resp = gateway.complete(request)  # 返回 LLMResponse

    测试方式：
        mock_service = MockAIService()
        gateway = LLMGateway(service=mock_service)
    """

    def __init__(self, service: AIService | None = None) -> None:
        # 允许注入 service（测试 / 固定 provider 场景）；
        # 为 None 时每次请求通过工厂获取默认服务。
        self._injected: AIService | None = service

    @property
    def provider(self) -> str:
        """当前使用的 provider 名称（供诊断 / 展示）。"""
        if self._injected is not None:
            return self._injected.name
        cfg = load_llm_config()
        import os

        return (
            os.environ.get("EIDOLON_LLM_PROVIDER")
            or cfg.get("provider")
            or "deepseek"
        )

    def complete(self, request: LLMRequest) -> LLMResponse:
        """同步补全：发送 request.messages，返回 LLMResponse。

        如果 request 中指定了 temperature / max_tokens，
        尝试创建带这些参数的服务实例；
        底层服务若不支持则忽略，使用自身配置。
        """
        service = self._resolve_service(request)
        try:
            content = service.chat(request.messages, stream=request.stream)
        except LLMUnconfigured:
            raise
        except LLMError:
            raise
        return LLMResponse(
            content=content,
            provider=getattr(service, "name", ""),
        )

    def complete_stream(self, request: LLMRequest) -> Iterator[LLMStreamChunk]:
        """流式补全（未来扩展）。

        当前底层服务尚未实现真正的流式输出，
        此方法以单个分块返回完整结果，保持接口兼容。
        """
        service = self._resolve_service(request)
        content = service.chat(request.messages, stream=True)
        yield LLMStreamChunk(delta=content, finish_reason="stop")

    # ---- 内部 ----

    def _resolve_service(self, request: LLMRequest) -> AIService:
        """根据 request 解析底层服务实例。

        优先级：
        1. 注入的 service（测试 / 固定）
        2. 如果 request 带 per-request 参数，创建带参数的服务实例
        3. 工厂默认服务（读 config.toml / 环境变量）
        """
        if self._injected is not None:
            return self._injected

        # 如果 request 指定了 per-request 参数，尝试创建带参数的服务
        has_overrides = (
            request.temperature is not None or request.max_tokens is not None
        )
        if has_overrides:
            return self._create_service_with_overrides(request)
        return get_service()

    @staticmethod
    def _create_service_with_overrides(
        request: LLMRequest,
    ) -> AIService:
        """创建一个带 per-request 参数覆盖的服务实例。

        从 config.toml / 环境变量读取基础配置，
        再用 request 中的参数覆盖 temperature / max_tokens。
        """
        import os

        cfg = load_llm_config()
        provider = (
            os.environ.get("EIDOLON_LLM_PROVIDER")
            or cfg.get("provider")
            or "deepseek"
        )
        kwargs: dict = {}
        for f in ("api_key", "base_url", "model"):
            val = os.environ.get("EIDOLON_LLM_" + f.upper()) or cfg.get(f)
            if val is not None:
                kwargs[f] = val
        # per-request 覆盖
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if provider not in _default_factory._registry:
            raise LLMError(
                f"未知 AI 服务：{provider!r}。"
                f"已注册：{sorted(_default_factory._registry)}"
            )
        return _default_factory._registry[provider](**kwargs)
