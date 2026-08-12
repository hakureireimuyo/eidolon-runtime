"""Context IR —— 上下文中间表示。

设计对齐 docs/context-management.md §5（编译管线）和 §6（缓存友好布局）。

核心概念：
- ContextSegment: 一段有语义标签的文本片段，带稳定性层级。
- ContextLayer: 稳定性层级枚举，数值越小越稳定（越靠前排列）。
- ContextIR: 多个 segment 组成的中间表示，可按层分组、排序。

IR 的价值（摘自设计文档）：
  有了 IR 之后，可以独立决定哪些进 prompt、哪些进工具调用、
  哪些进隐藏状态、哪些进长期记忆——而无需修改 Processor。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator


class ContextLayer(IntEnum):
    """上下文稳定性分层。

    数值越小 = 越稳定 = 越靠前排列（缓存友好的前缀越长）。
    对齐 docs/context-management.md §2 的四层模型。
    """

    STATIC = 0  # 世界观、角色基础人格、行为规则 —— 几乎不变
    LOW = 1  # 时间、季节、社会环境、长期关系 —— 天/月级
    MID = 2  # 当前任务、近期事件、关系状态、目标 —— 小时/天级
    HIGH = 3  # 当前情绪、短期记忆、对话上下文 —— 每轮对话

    @property
    def label(self) -> str:
        return _LAYER_LABELS[self]


_LAYER_LABELS: dict[ContextLayer, str] = {
    ContextLayer.STATIC: "静态层",
    ContextLayer.LOW: "低频层",
    ContextLayer.MID: "中频层",
    ContextLayer.HIGH: "高频层",
}


@dataclass
class ContextSegment:
    """一个上下文片段。

    text: 片段文本内容。
    layer: 稳定性层级（决定排列顺序 / 缓存策略）。
    tag: 语义标签（如 "character_prompt"、"emotion"、"conversation_turn"）。
    role: 消息角色（"system" / "user" / "assistant"），默认 "system"。
         非 system 角色的片段直接作为独立 message 输出（对话历史）。
    cacheable: 是否参与前缀缓存（True = 稳定可复用）。
    """

    text: str
    layer: ContextLayer
    tag: str
    role: str = "system"
    cacheable: bool = True

    def __post_init__(self) -> None:
        if not self.tag:
            raise ValueError("ContextSegment.tag 不能为空")
        if not isinstance(self.layer, ContextLayer):
            raise TypeError(
                f"layer 必须是 ContextLayer 枚举，得到 {type(self.layer).__name__}"
            )


@dataclass
class ContextIR:
    """上下文中间表示 —— 多个 segment 的集合。

    对齐设计文档的 Context IR 概念：
    Processor 输出 {facts, constraints, intentions, memories}
    → Context IR → Token Layout → Model Input
    """

    segments: list[ContextSegment] = field(default_factory=list)

    def add(self, segment: ContextSegment) -> None:
        self.segments.append(segment)

    def replace_tag(self, tag: str, segment: ContextSegment) -> None:
        """替换同 tag 的旧 segment；不存在则追加。"""
        for i, s in enumerate(self.segments):
            if s.tag == tag:
                self.segments[i] = segment
                return
        self.segments.append(segment)

    def remove_tag(self, tag: str) -> None:
        self.segments = [s for s in self.segments if s.tag != tag]

    def find(self, tag: str) -> ContextSegment | None:
        for s in self.segments:
            if s.tag == tag:
                return s
        return None

    def by_layer(self) -> dict[ContextLayer, list[ContextSegment]]:
        """按层分组，同层内保持插入顺序。"""
        result: dict[ContextLayer, list[ContextSegment]] = {
            layer: [] for layer in ContextLayer
        }
        for s in self.segments:
            result[s.layer].append(s)
        return result

    def sorted_segments(self) -> list[ContextSegment]:
        """按稳定性排序（static → high），同层保持插入顺序。"""
        return sorted(self.segments, key=lambda s: (int(s.layer),))

    @property
    def total_text_length(self) -> int:
        return sum(len(s.text) for s in self.segments)

    def __iter__(self) -> Iterator[ContextSegment]:
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)
