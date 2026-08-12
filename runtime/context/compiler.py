"""ContextCompiler —— 上下文编译器。

将 ContextIR 编译为 LLM 可直接消费的 messages 列表。

编译规则(对齐 docs/context-management.md §6 缓存友好布局):
1. 按稳定性排序:static → low → mid → high
2. system 角色的片段合并为一条 system message(保证前缀稳定)
3. 非 system 角色(user / assistant)的片段直接作为独立 message 输出
4. 对话缓冲区的消息追加在末尾(高频层,每轮变化)

布局示意:
  [system: static + low + mid 合并]   ← 前缀缓存区域(多轮间不变)
  [user: ...]                         ← 对话历史
  [assistant: ...]
  [user: 当前输入]                     ← 本次新增

这样 LLM 的前缀缓存(KV Cache)在多轮对话间可复用 system 部分,
只有尾部对话区每轮追加新消息。
"""
from __future__ import annotations

from .ir import ContextIR, ContextLayer, ContextSegment
from .buffer import ConversationBuffer


class ContextCompiler:
    """将 ContextIR + ConversationBuffer 编译为 messages 列表。

    无状态编译器:不持有任何可变数据,可安全共享。
    """

    def compile(
        self,
        ir: ContextIR,
        conversation: ConversationBuffer | None = None,
    ) -> list[dict]:
        """编译上下文为 OpenAI 风格 messages 列表。

        参数:
        ir: 上下文中间表示(各层 segment)。
        conversation: 对话缓冲区(高频层),其消息追加在 system 之后。
                      传入 None 则不含对话历史。

        返回:[{"role": "system", "content": "..."},
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": "..."},
                ...]
        """
        messages: list[dict] = []

        # 1. 收集 system 角色片段,按稳定性合并
        system_parts: list[str] = []
        non_system_segments: list[ContextSegment] = []

        for seg in ir.sorted_segments():
            if seg.role == "system":
                system_parts.append(seg.text)
            else:
                non_system_segments.append(seg)

        # 2. 合并 system 片段为一条 system message
        if system_parts:
            messages.append(
                {"role": "system", "content": "\n\n".join(system_parts)}
            )

        # 3. 追加非 system 片段(按稳定性排序)
        for seg in non_system_segments:
            messages.append({"role": seg.role, "content": seg.text})

        # 4. 追加对话历史(高频层,最动态的部分)
        if conversation is not None:
            messages.extend(conversation.to_messages())

        return messages

    def compile_prefix(
        self,
        ir: ContextIR,
    ) -> str:
        """只编译 system 前缀部分(用于缓存预检 / 诊断)。

        返回 static + low + mid 层的 system 文本合并结果。
        """
        system_parts: list[str] = []
        for seg in ir.sorted_segments():
            if seg.role != "system":
                continue
            if seg.layer == ContextLayer.HIGH:
                continue
            system_parts.append(seg.text)
        return "\n\n".join(system_parts)

    def estimate_cache_boundary(
        self,
        ir: ContextIR,
        conversation: ConversationBuffer | None = None,
    ) -> dict:
        """估算缓存边界(哪些部分可复用前缀)。

        返回:
        {
            "prefix_segments": ["character_prompt", ...],  # 可缓存 segment 标签
            "dynamic_segments": ["emotion", ...],            # 需要重算的 segment 标签
            "conversation_turns": 5,                         # 对话轮数
        }
        """
        prefix_tags: list[str] = []
        dynamic_tags: list[str] = []

        for seg in ir.sorted_segments():
            if seg.role == "system" and seg.layer <= ContextLayer.MID:
                prefix_tags.append(seg.tag)
            else:
                dynamic_tags.append(seg.tag)

        return {
            "prefix_segments": prefix_tags,
            "dynamic_segments": dynamic_tags,
            "conversation_turns": len(conversation) if conversation else 0,
        }
