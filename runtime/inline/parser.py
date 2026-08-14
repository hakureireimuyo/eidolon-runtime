"""StreamParser —— 内联协议解析层(LLM 与程序之间的沟通协议)。

设计要点(对齐 docs/streaming-event-loop-placeholder.md §4):
- **只认语法,不认识任何程序**:解析层只负责 `⟦ ⟧` 文法;注入应答由上层
  注入的 resolver 完成(返回 None = 程序未接入 / 路径未知)。
- **解析失败静默替换**:未知 kind / 未注册路径 / 未闭合 / 缓冲超限,
  一律替换为空,不输出任何内容——残次符号永不到达用户。
- **截断**:进入占位符后原始内容一律不发;指令(⟦b:…⟧)风格在开标签即知,
  内容渐进透传;注入(⟦time⟧)缓冲至闭合,解析值以单次 delta 发出。
- **V1 无嵌套、无递归**:第一个 ⟧ 即闭合,解析结果不再二次解析。
- 同一实例跨轮次复用(AgentLoop 每轮流式文本都 feed 进同一解析器),
  累积的解析后纯文本与 segments 即最终入史内容。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

#: 解析失败(未接入 / 未知路径 / 未闭合 / 超限)一律静默替换为空。
Resolver = Callable[[str, Any], Optional[str]]

OPEN = "⟦"   # ⟦ U+27E6 MATHEMATICAL LEFT WHITE SQUARE BRACKET
CLOSE = "⟧"  # ⟧ U+27E7 MATHEMATICAL RIGHT WHITE SQUARE BRACKET

#: 指令 kind → 前端样式(V1 仅粗体 / 斜体)。
STYLES = {"b": "bold", "i": "italic"}

#: 注入缓冲上限(字符数),超限视为模型失控,静默丢弃。
MAX_BUFFER = 512


@dataclass
class InlineEvent:
    """解析层产出的事件(供上层转协议事件 / 前端渲染)。"""

    delta: str
    style: str = "plain"  # "plain" | "bold" | "italic"


@dataclass
class _Span:
    """一个占位符的解析状态。"""

    buf: list[str] = field(default_factory=list)  # 已缓冲的原始字符
    kind: Optional[str] = None  # 指令 kind(b/i),None = 注入路径
    style: str = "plain"


class StreamParser:
    """⟦ ⟧ 增量状态机:流式文本进,解析后事件出。

    使用方式:
        parser = StreamParser(resolver=registry.resolve, ctx=session)
        for ev in parser.feed(chunk):
            ...  # InlineEvent(delta, style)
        parser.finish()          # 流结束(未闭合占位符静默丢弃)
        text = parser.resolved_text   # 解析后纯文本(入史内容)
        segs = parser.segments        # [{text, style}](前端渲染)
    """

    def __init__(
        self,
        resolver: Resolver | None = None,
        ctx: Any = None,
        *,
        max_buffer: int = MAX_BUFFER,
    ) -> None:
        # 未注入 resolver(程序未接入)时,一切注入静默替换为空
        self._resolver: Resolver = resolver or (lambda path, ctx: None)
        self._ctx = ctx
        self._max_buffer = max_buffer
        self._plain: list[str] = []      # 解析后纯文本累积(样式剥离)
        self._segments: list[dict] = []  # [{text, style}],相邻同风格合并
        self._span: Optional[_Span] = None  # None = plain 态

    # ---- 输入 ----

    def feed(self, text: str) -> list[InlineEvent]:
        """喂入一段流式文本,返回解析后的事件列表。"""
        events: list[InlineEvent] = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if self._span is None:
                i = self._feed_plain(text, i, events)
            else:
                i = self._feed_span(text, i, events)
        return events

    def finish(self) -> list[InlineEvent]:
        """流结束:未闭合占位符静默丢弃,缓冲清空。"""
        self._span = None
        return []

    # ---- 输出(入史 / 渲染) ----

    @property
    def resolved_text(self) -> str:
        """解析后纯文本(值已替换、样式剥离)——轮末入史的唯一内容。"""
        return "".join(self._plain)

    @property
    def segments(self) -> list[dict]:
        """解析后带样式片段(前端渲染)。"""
        return list(self._segments)

    # ---- 内部 ----

    def _feed_plain(
        self, text: str, i: int, events: list[InlineEvent]
    ) -> int:
        """plain 态:遇 ⟦ 进入缓冲;转义符处理;其余批量透传。"""
        ch = text[i]
        if ch == "\\":
            if i + 1 < len(text) and text[i + 1] in (OPEN, CLOSE):
                # 转义:字面符号
                self._emit(events, text[i + 1], "plain")
                return i + 2
            self._emit(events, "\\", "plain")
            return i + 1
        if ch == OPEN:
            self._span = _Span()
            return i + 1
        j = i
        while j < len(text) and text[j] not in (OPEN, CLOSE, "\\"):
            j += 1
        if j > i:
            self._emit(events, text[i:j], "plain")
        return j

    def _feed_span(
        self, text: str, i: int, events: list[InlineEvent]
    ) -> int:
        """占位符内:闭合判定 / 指令渐进透传 / 注入缓冲 / 超限静默丢弃。"""
        assert self._span is not None
        span = self._span
        ch = text[i]
        if ch == CLOSE:
            self._close(events)
            return i + 1
        if span.kind is not None:
            # 指令已判定:风格在开标签即知,内容渐进透传
            self._emit(events, ch, span.style)
            return i + 1
        # 未判定(注入或指令前缀):缓冲;内层 ⟦ 视为内容字符
        span.buf.append(ch)
        self._classify()
        if span.kind is None and len(span.buf) > self._max_buffer:
            # 模型失控:静默丢弃整个占位符
            self._span = None
        return i + 1

    def _classify(self) -> None:
        """收到分界符 ":" 时判定指令 kind;未知 kind / 注入保持缓冲。"""
        assert self._span is not None
        span = self._span
        if not span.buf or span.buf[-1] != ":":
            return
        kind = "".join(span.buf[:-1])
        style = STYLES.get(kind)
        if style is None:
            # 未知 kind:保持缓冲,闭合时静默丢弃(见 _close)
            return
        span.kind = kind
        span.style = style

    def _close(self, events: list[InlineEvent]) -> None:
        """闭合占位符:指令收尾;注入解析替换;未知 / 未接入静默丢弃。"""
        assert self._span is not None
        span = self._span
        body = "".join(span.buf)
        if span.kind is not None and span.kind in STYLES:
            # 指令:已渐进透传,闭合无额外输出
            self._span = None
            return
        # 注入路径(含未知 kind 的 : 形式):解析替换
        value = self._resolver(body, self._ctx) if body else None
        self._span = None
        if value:
            self._emit(events, value, "plain")

    def _emit(
        self, events: list[InlineEvent], text: str, style: str
    ) -> None:
        """发出解析后文本:进入事件列表 + 纯文本累积 + 片段合并。"""
        events.append(InlineEvent(delta=text, style=style))
        self._plain.append(text)
        if self._segments and self._segments[-1]["style"] == style:
            self._segments[-1]["text"] += text
        else:
            self._segments.append({"text": text, "style": style})
