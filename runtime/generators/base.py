"""生成器层 —— LLM 调度的功能封装。

每个生成器是一个独立可输入输出的对象,**自管理自己的 ContextManager**:
- 角色对话生成器(dialogue):用户消息 → 角色回复;
- 剧情描写生成器(narrative,未来):玩家选择/输出 → 剧情文本;
- 环境生成器(environment,未来):世界环境数据 → 环境描述。

消费层(RuntimeEngine)不再直接触碰上下文 / 网关,只按生成器接口收发
数据。生成器之间上下文交叉输入是未来事项:构造已留 context 注入位,
届时可将上游生成器的上下文(或快照)注入下游生成器。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..context import ContextManager
from ..llm_gateway import LLMGateway


class Generator(ABC):
    """生成器抽象:自管理上下文,输入 payload → 输出 result。

    - gateway:由引擎注入(引擎内共享同一无状态实例);
    - context:缺省自建(每生成器一份);可注入用于测试 mock 或
      未来生成器间上下文交叉输入;
    - generate:同步生成,stream 为流式扩展位(本次恒 False,真流式
      未来以独立 stream_generate() 实现,不破坏同步契约)。
    """

    id: str = ""     # 唯一标识,如 "dialogue";未来 "narrative"/"environment"
    label: str = ""  # 界面显示名(中文)

    def __init__(
        self,
        *,
        gateway: LLMGateway | None = None,
        context: ContextManager | None = None,
    ) -> None:
        self._gateway = gateway or LLMGateway()
        self._context = context or ContextManager()
        self._configure()

    def _configure(self) -> None:
        """子类钩子:构造时注入静态上下文(如角色 system prompt)。"""

    @abstractmethod
    def generate(self, payload: dict, *, stream: bool = False) -> dict:
        """同步生成:输入 payload → 输出 result(必含主文本字段)。

        stream 为流式扩展位,本次实现恒为 False。
        """

    @property
    def context(self) -> ContextManager:
        """生成器自管理的上下文(开发者工具经此只读诊断)。"""
        return self._context

    def reset(self) -> None:
        """清空对话历史(保留静态层等上下文片段)。"""
        self._context.reset_conversation()
