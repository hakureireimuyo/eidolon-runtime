"""内联协议解析层 —— LLM 与程序之间的沟通协议。

解析层只认 `⟦ ⟧` 文法,不认识任何程序;注入应答由上层注入的
InterpreterRegistry 完成,未接入即静默替换为空。
"""
from __future__ import annotations

from .parser import (
    CLOSE,
    MAX_BUFFER,
    OPEN,
    STYLES,
    InlineEvent,
    StreamParser,
)
from .registry import InterpreterRegistry

__all__ = [
    "StreamParser",
    "InlineEvent",
    "InterpreterRegistry",
    "OPEN",
    "CLOSE",
    "STYLES",
    "MAX_BUFFER",
]
