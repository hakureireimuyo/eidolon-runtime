"""ContextManager —— 上下文管理器。

eidolon-runtime 通过此接口管理上下文,不直接操作 messages 拼接。

核心职责:
1. **分层管理**:按稳定性(static / low / mid / high)维护各层上下文片段。
2. **增量更新**:只更新变化的片段,不重建全文(对齐 §4 "状态演化")。
3. **缓存友好**:编译时按稳定性排序,保证 LLM 前缀缓存命中(对齐 §6)。
4. **对话缓冲**:管理高频对话历史,支持窗口截断。
5. **编译代理**:通过 ContextCompiler 将 IR + 对话 → messages 列表。

与 LLM Gateway 的协作:
    ContextManager.compile() → messages
    → LLMGateway.complete(LLMRequest(messages)) → LLMResponse
    → ContextManager.add_message("assistant", response.content)

多 agent 场景:
    每个 agent 持有自己的 ContextManager 实例,
    共享的 state 由各能力子项目通过事件总线更新。
"""
from __future__ import annotations

from .ir import ContextIR, ContextLayer, ContextSegment
from .buffer import ConversationBuffer
from .compiler import ContextCompiler


class ContextManager:
    """上下文管理器 —— eidolon-runtime 的上下文抽象入口。

    使用方式:
        mgr = ContextManager()
        mgr.set_static("character_prompt", system_prompt)
        mgr.add_message("user", "你好")
        messages = mgr.compile()
        # → 交给 LLMGateway
        mgr.add_message("assistant", reply)
    """

    def __init__(
        self,
        *,
        max_conversation_turns: int = 40,
        compiler: ContextCompiler | None = None,
    ) -> None:
        self._ir = ContextIR()
        self._conversation = ConversationBuffer(max_turns=max_conversation_turns)
        self._compiler = compiler or ContextCompiler()
        # system 前缀缓存(L0/L1 级)
        self._cached_prefix: str | None = None
        self._prefix_dirty: bool = True

    # ---- 上下文片段管理 ----

    def set_static(self, tag: str, text: str) -> None:
        """设置 / 更新静态层片段(世界观、角色人格、行为规则等)。"""
        self._set_segment(tag, text, ContextLayer.STATIC)

    def set_low(self, tag: str, text: str) -> None:
        """设置 / 更新低频层片段(时间、季节、社会环境等)。"""
        self._set_segment(tag, text, ContextLayer.LOW)

    def set_mid(self, tag: str, text: str) -> None:
        """设置 / 更新中频层片段(关系状态、当前目标、近期事件等)。"""
        self._set_segment(tag, text, ContextLayer.MID)

    def set_high(self, tag: str, text: str) -> None:
        """设置 / 更新高频层片段(当前情绪、短期记忆等)。

        注意:对话历史通过 add_message() 管理,不通过此方法。
        此方法用于非对话的高频状态片段(如情绪描述)。
        """
        self._set_segment(tag, text, ContextLayer.HIGH)

    def remove(self, tag: str) -> None:
        """移除指定 tag 的片段。"""
        seg = self._ir.find(tag)
        if seg is not None and seg.role == "system" and seg.layer <= ContextLayer.MID:
            self._prefix_dirty = True
        self._ir.remove_tag(tag)

    def get_segment(self, tag: str) -> ContextSegment | None:
        """查询指定 tag 的片段(只读)。"""
        return self._ir.find(tag)

    # ---- 对话管理 ----

    def add_message(self, role: str, content: str) -> None:
        """追加一条对话消息(高频层)。"""
        self._conversation.add(role, content)

    def reset_conversation(self) -> None:
        """清空对话历史(保留其他层片段不变)。"""
        self._conversation.clear()

    @property
    def conversation_turns(self) -> list:
        """当前对话历史(只读副本)。"""
        return self._conversation.turns

    @property
    def conversation_max_turns(self) -> int:
        return self._conversation.max_turns

    @conversation_max_turns.setter
    def conversation_max_turns(self, value: int) -> None:
        self._conversation.max_turns = value

    # ---- 编译 ----

    def compile(self) -> list[dict]:
        """编译当前上下文为 OpenAI 风格 messages 列表。

        这是 ContextManager 的核心输出——把分层管理的片段 + 对话历史
        编译为 LLM 可消费的 messages,布局为缓存友好(稳定在前,动态在后)。
        """
        return self._compiler.compile(self._ir, self._conversation)

    def compile_prefix(self) -> str:
        """只编译 system 前缀(static + low + mid 合并)。"""
        if self._prefix_dirty or self._cached_prefix is None:
            self._cached_prefix = self._compiler.compile_prefix(self._ir)
            self._prefix_dirty = False
        return self._cached_prefix

    def cache_info(self) -> dict:
        """返回缓存状态信息(供诊断 / 性能优化)。"""
        return self._compiler.estimate_cache_boundary(self._ir, self._conversation)

    # ---- 状态重置 ----

    def clear_all(self) -> None:
        """清空所有上下文(包括静态层和对话历史)。"""
        self._ir = ContextIR()
        self._conversation.clear()
        self._cached_prefix = None
        self._prefix_dirty = True

    # ---- 内部 ----

    def _set_segment(
        self, tag: str, text: str, layer: ContextLayer
    ) -> None:
        """设置 / 更新指定层的片段。同 tag 自动替换。"""
        existing = self._ir.find(tag)
        seg = ContextSegment(text=text, layer=layer, tag=tag)
        if existing is not None:
            self._ir.replace_tag(tag, seg)
        else:
            self._ir.add(seg)
        # system 角色的低层片段变化 → 前缀缓存失效
        if layer <= ContextLayer.MID:
            self._prefix_dirty = True

    @property
    def ir(self) -> ContextIR:
        """暴露 IR(只读视角,供高级用法 / 测试)。"""
        return self._ir
