"""工具层 —— LLM 反向指挥程序的执行接口。

函数调用语义一律走工具(与内嵌占位符分工:占位符只做样式与只读值引用)。
"""
from __future__ import annotations

from .registry import ToolError, ToolRegistry, ToolSpec

__all__ = ["ToolRegistry", "ToolSpec", "ToolError"]
