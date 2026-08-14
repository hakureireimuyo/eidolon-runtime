"""ConversationBuffer —— 对话缓冲区(高频层)。

管理 recent messages,支持:
- 追加新消息(user / assistant)
- 窗口截断(只保留最近 N 轮,避免无限增长)
- 导出为 OpenAI 风格 messages 列表

对齐 docs/state-and-context.md §4 的"高频层"设计:
"每轮对话更新,局部更新,不重建全部"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ConversationTurn:
    """一轮对话消息。

    content: 解析后纯文本(值已替换、样式剥离)——入史即固定,永不重解析;
    segments: 解析后带样式片段(前端渲染用),可为 None。
    """

    role: str  # "user" | "assistant"
    content: str
    segments: list | None = None
    ts: float = field(default_factory=time.time)


class ConversationBuffer:
    """对话历史缓冲区。

    max_turns: 保留的最大轮数(1 轮 = 1 条消息)。
    超出时自动从最旧的消息开始截断。
    设为 0 表示无限制(不推荐生产环境使用)。
    """

    def __init__(self, max_turns: int = 40) -> None:
        self._turns: list[ConversationTurn] = []
        self._max_turns = max_turns

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @max_turns.setter
    def max_turns(self, value: int) -> None:
        self._max_turns = max(0, value)
        self._evict()

    def add(
        self, role: str, content: str, segments: list | None = None
    ) -> ConversationTurn:
        """追加一条消息,返回创建的 turn。"""
        if role not in ("user", "assistant", "system"):
            raise ValueError(f"role 必须是 user/assistant/system,得到 {role!r}")
        turn = ConversationTurn(role=role, content=content, segments=segments)
        self._turns.append(turn)
        self._evict()
        return turn

    def to_messages(self) -> list[dict]:
        """导出为 OpenAI 风格消息列表。"""
        return [{"role": t.role, "content": t.content} for t in self._turns]

    @property
    def turns(self) -> list[ConversationTurn]:
        return list(self._turns)

    def __len__(self) -> int:
        return len(self._turns)

    def clear(self) -> None:
        self._turns.clear()

    def _evict(self) -> None:
        """如果超过 max_turns,从最旧的消息开始截断。"""
        if self._max_turns > 0 and len(self._turns) > self._max_turns:
            excess = len(self._turns) - self._max_turns
            self._turns = self._turns[excess:]
