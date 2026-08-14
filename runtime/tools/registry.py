"""ToolRegistry —— 工具注册表(程序接口)。

函数调用语义(有副作用 / 结构化参数 / 需校验 / 需模型基于结果推理)
一律走工具:LLM 通过 provider 原生 function calling 发出请求,引擎执行,
结果以 tool 消息回流(对齐 docs/streaming-event-loop-placeholder.md §5)。

- ToolSpec:工具的完整声明(JSON Schema + 执行器 + 展示标签);
- ToolRegistry:注册与执行;未知工具 / 执行异常 → 文本结果回流模型,
  由模型自行挽救(不回滚世界)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

#: executor 签名:参数 dict → 结果文本(回流给模型)。
ToolExecutor = Callable[[dict], str]


class ToolError(Exception):
    """工具执行错误(未知工具 / 执行异常;文本回流模型,不中断循环)。"""


@dataclass
class ToolSpec:
    """一个工具的完整声明。"""

    name: str
    description: str
    parameters: dict  # JSON Schema(object)
    executor: ToolExecutor
    label: str = ""  # 前端展示名(中文);空则用 name

    def to_openai(self) -> dict:
        """转 OpenAI 工具声明格式(provider 原生 function calling)。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """按名注册工具;V1 注册表可为空——协议与循环先行,工具逐步接入。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具(同名覆盖)。"""
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def to_openai(self, names: list[str] | None = None) -> list[dict]:
        """转 OpenAI 工具声明列表;names 为允许集(None = 全部)。"""
        selected = (
            [n for n in names if n in self._tools]
            if names is not None
            else self.names()
        )
        return [self._tools[n].to_openai() for n in selected]

    def execute(self, name: str, args: dict) -> str:
        """执行工具,返回结果文本;失败时抛 ToolError(回流模型)。"""
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(f"未注册工具:{name!r}")
        try:
            result = spec.executor(args or {})
        except Exception as exc:  # noqa: BLE001 - 错误文本回流模型
            raise ToolError(f"工具 {name} 执行失败:{exc}") from exc
        return str(result)
