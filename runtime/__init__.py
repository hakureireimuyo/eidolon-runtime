"""Eidolon Runtime —— 运行时层（V1）。

职责（见 docs/project-responsibilities.md §3.4）：
- 消费 PersonaSeed 包、用 eidolon-character 解释角色身份模块
- 维护运行时对话状态，驱动角色对话

不重新定义数据格式（那是扩展层职责），不重实现容器逻辑（那是 PersonaSeed 职责）。
"""

__version__ = "0.1.0"
