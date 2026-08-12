"""Eidolon Runtime —— 运行时层。

职责（见 docs/project-responsibilities.md §3.4）：
- 消费 PersonaSeed 包、用 eidolon-character 解释角色身份模块
- 维护运行时对话状态，驱动角色对话

架构包含两层核心抽象（多 agent 底层支撑）：
- runtime.llm_gateway: LLM 抽象层，封装 provider 差异，输入→输出
- runtime.context: 上下文管理层，分层缓存友好布局，KV cache 优化

不重新定义数据格式（那是扩展层职责），不重实现容器逻辑（那是 PersonaSeed 职责）。
"""

__version__ = "0.2.0"
