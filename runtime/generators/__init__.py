"""生成器层 —— LLM 调度的功能封装(独立可输入输出的对象)。

位于消费层(RuntimeEngine)与 LLM 调度(LLMGateway + ContextManager)
之间:每个生成器自管理自己的 ContextManager,消费层只按生成器接口
收发数据。当前实现:
- DialogueGenerator(角色对话);未来:narrative(剧情)/ environment(环境)。
"""
from .base import Generator
from .dialogue import DialogueGenerator

__all__ = ["Generator", "DialogueGenerator"]
