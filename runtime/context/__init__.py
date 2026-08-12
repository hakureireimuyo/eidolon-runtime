"""Context Management —— 上下文管理抽象层。

与 LLM Gateway 平级，是 eidolon-runtime 的两个核心抽象层之一。

职责（对齐 docs/context-management.md）：
- 分层管理上下文（static / low / mid / high）
- 增量更新（只改变化部分，不重建全文）
- 缓存友好布局（稳定在前，动态在后，最大化 KV cache 命中）
- 编译为 LLM 可消费的 messages 列表

不负责：
- 状态的内部表示（各能力子项目职责）
- LLM API 调用（LLM Gateway 职责）
- 记忆的存储和检索（记忆子项目职责）
"""
from __future__ import annotations

from .ir import ContextIR, ContextLayer, ContextSegment
from .buffer import ConversationBuffer, ConversationTurn
from .compiler import ContextCompiler
from .manager import ContextManager

__all__ = [
    "ContextManager",
    "ContextCompiler",
    "ContextIR",
    "ContextLayer",
    "ContextSegment",
    "ConversationBuffer",
    "ConversationTurn",
]
