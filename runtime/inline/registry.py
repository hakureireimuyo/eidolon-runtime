"""InterpreterRegistry —— 注入解析注册表(程序接入点)。

解析层(StreamParser)不认识任何程序:它只按协议发出注入路径,
由本注册表(由上层 / 各能力子项目装配)应答。

硬约束(对齐 docs/streaming-event-loop-placeholder.md §4.4):
- **纯只读**:resolver 无副作用、不改变任何状态;
- **参数简单**:path 仅提供者命名空间 + 简单标量,无结构化入参;
- **确定性替换**:值是「引用」(时间 / 名词),不需要模型基于值再推理。
违反约束的语义一律改为工具(tool calls)。

未注册路径返回 None → 解析层静默替换为空(程序未接入即静默)。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

#: resolver 签名:path → 解析值;None = 未接入 / 失败(静默替换)。
PathResolver = Callable[[str, Any], Optional[str]]


class InterpreterRegistry:
    """按路径注册注入解析器;LLM 只能指名已注册路径,不能发明执行。"""

    def __init__(self) -> None:
        self._resolvers: dict[str, PathResolver] = {}

    def register(self, path: str, resolver: PathResolver) -> None:
        """注册一个注入路径(如 "time" / "world:time" / "char:name")。"""
        if not path or path != path.strip():
            raise ValueError(f"非法注入路径:{path!r}")
        self._resolvers[path] = resolver

    def resolve(self, path: str, ctx: Any = None) -> Optional[str]:
        """解析路径;未注册 / resolver 异常 → None(静默替换为空)。"""
        resolver = self._resolvers.get(path)
        if resolver is None:
            return None
        try:
            value = resolver(ctx)
        except Exception:  # noqa: BLE001 - 解析失败静默替换
            return None
        if value is None:
            return None
        return str(value)

    def paths(self) -> list[str]:
        """已接入的注入路径(诊断用)。"""
        return sorted(self._resolvers)
