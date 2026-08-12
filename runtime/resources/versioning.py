"""版本兼容底座:语义化版本、范围表达式与迁移图。

Cartridge 约定:容器 `manifest.version` 是整数(由协议层自行迁移),
数据块 `entry.version` 是**字符串**——模块自身的 Schema 版本。本模块只处理后者。

三条兼容策略(由 router 依次尝试,任何一条命中都不会让整包加载失败):

1. **命中范围**:数据版本落在 handler 声明的支持范围内,直接加载。
2. **迁移链**:注册过 migration 时,用 BFS 找一条最短升级路径,逐跳改写数据。
3. **宽容降级**:找不到路径时——数据比 handler 新则按"前向兼容"加载
   (未知字段原样保留),数据比 handler 旧则按"降级"加载；两者都失败才退回
   通用动态资源。老运行时读新数据、新运行时读老数据都不会崩。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from . import typespec

_VERSION_RE = re.compile(
    r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]([0-9A-Za-z.\-]+))?\s*$"
)


@dataclass(frozen=True)
class Version:
    """宽容的语义化版本。无法解析的输入退化为 0.0.0 并保留原文。"""

    major: int = 0
    minor: int = 0
    patch: int = 0
    pre: str = ""
    raw: str = ""
    parsed: bool = True

    @classmethod
    def parse(cls, value: Any) -> "Version":
        if isinstance(value, Version):
            return value
        if value is None or value == "":
            return cls(0, 0, 0, "", "", parsed=False)
        if isinstance(value, int):
            return cls(value, 0, 0, "", str(value))
        if isinstance(value, float):
            value = str(value)
        text = str(value).strip()
        m = _VERSION_RE.match(text)
        if not m:
            return cls(0, 0, 0, "", text, parsed=False)
        return cls(
            major=int(m.group(1)),
            minor=int(m.group(2) or 0),
            patch=int(m.group(3) or 0),
            pre=m.group(4) or "",
            raw=text,
        )

    @property
    def key(self) -> tuple:
        # 预发布版本排在同号正式版之前
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, self.pre)

    def __lt__(self, other: "Version") -> bool:
        return self.key < other.key

    def __le__(self, other: "Version") -> bool:
        return self.key <= other.key

    def __gt__(self, other: "Version") -> bool:
        return self.key > other.key

    def __ge__(self, other: "Version") -> bool:
        return self.key >= other.key

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            base += f"-{self.pre}"
        return base

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Version({self.raw or str(self)!r})"


_CLAUSE_RE = re.compile(r"^(>=|<=|==|!=|>|<|\^|~)?\s*(.+)$")


@dataclass
class VersionRange:
    """版本范围表达式。

    支持:`*` / `1.2.3` / `>=1.0,<2.0` / `^1.2`(同大版本) / `~1.2`(同小版本) /
    `1.x`(同大版本)。多个子句以逗号或空格分隔,取交集。
    """

    clauses: list[tuple[str, "Version"]] = field(default_factory=list)
    raw: str = "*"

    @classmethod
    def parse(cls, spec: Any) -> "VersionRange":
        if isinstance(spec, VersionRange):
            return spec
        if spec is None or str(spec).strip() in ("", "*", "any"):
            return cls([], "*")
        text = str(spec).strip()
        clauses: list[tuple[str, Version]] = []
        for token in re.split(r"[,\s]+", text):
            if not token:
                continue
            m = _CLAUSE_RE.match(token)
            if not m:
                continue
            op, ver_text = m.group(1) or "==", m.group(2)
            if ver_text.endswith((".x", ".*")):
                # 1.x -> >=1.0.0,<2.0.0 ; 1.2.x -> >=1.2.0,<1.3.0
                head = ver_text[:-2].rstrip(".")
                base = Version.parse(head)
                if head.count(".") == 0:
                    clauses.append((">=", base))
                    clauses.append(("<", Version(base.major + 1, 0, 0)))
                else:
                    clauses.append((">=", base))
                    clauses.append(("<", Version(base.major, base.minor + 1, 0)))
                continue
            ver = Version.parse(ver_text)
            if op == "^":
                clauses.append((">=", ver))
                clauses.append(("<", Version(ver.major + 1, 0, 0)))
            elif op == "~":
                clauses.append((">=", ver))
                clauses.append(("<", Version(ver.major, ver.minor + 1, 0)))
            else:
                clauses.append((op, ver))
        return cls(clauses, text)

    def contains(self, version: Any) -> bool:
        v = Version.parse(version)
        if not self.clauses:
            return True
        for op, target in self.clauses:
            if op == ">=" and not v >= target:
                return False
            if op == ">" and not v > target:
                return False
            if op == "<=" and not v <= target:
                return False
            if op == "<" and not v < target:
                return False
            if op == "==" and v.key != target.key:
                return False
            if op == "!=" and v.key == target.key:
                return False
        return True

    @property
    def lower(self) -> Optional[Version]:
        """最严格的下界(用于判断"数据比 handler 旧")。"""
        bounds = [v for op, v in self.clauses if op in (">=", ">", "==")]
        return max(bounds, key=lambda v: v.key) if bounds else None

    @property
    def upper(self) -> Optional[Version]:
        """最严格的上界(用于判断"数据比 handler 新")。"""
        bounds = [v for op, v in self.clauses if op in ("<=", "<", "==")]
        return min(bounds, key=lambda v: v.key) if bounds else None

    def __str__(self) -> str:
        return self.raw


# 迁移函数签名:fn(data, context) -> data
MigrationFn = Callable[..., Any]


@dataclass
class Migration:
    """一条版本迁移边:把匹配 `from_range` 的数据改写为 `to_version`。"""

    type_pattern: str
    from_range: VersionRange
    to_version: Version
    fn: MigrationFn
    note: str = ""

    def apply(self, data: Any, context: Any = None) -> Any:
        try:
            return self.fn(data, context)
        except TypeError:
            # 允许只接收 data 的单参迁移函数
            return self.fn(data)

    def label(self) -> str:
        return f"{self.type_pattern} {self.from_range} -> {self.to_version}"


class MigrationGraph:
    """按类型标签组织的迁移边集合,用 BFS 规划最短升级路径。"""

    def __init__(self) -> None:
        self._edges: list[Migration] = []

    def add(
        self,
        type_pattern: str,
        from_versions: Any,
        to_version: Any,
        fn: MigrationFn,
        note: str = "",
    ) -> Migration:
        mig = Migration(
            type_pattern=type_pattern,
            from_range=VersionRange.parse(from_versions),
            to_version=Version.parse(to_version),
            fn=fn,
            note=note,
        )
        self._edges.append(mig)
        return mig

    def edges_for(self, type_value: str) -> list[Migration]:
        scored = [
            (typespec.match_score(e.type_pattern, type_value), e) for e in self._edges
        ]
        return [e for score, e in scored if score is not None]

    def plan(
        self, type_value: str, source: Any, target: VersionRange, max_steps: int = 16
    ) -> Optional[list[Migration]]:
        """找一条把 `source` 版本带进 `target` 范围的迁移链。

        返回 [](已在范围内)/ 迁移列表 / None(无路径)。
        """
        start = Version.parse(source)
        if target.contains(start):
            return []
        edges = self.edges_for(type_value)
        if not edges:
            return None
        seen = {str(start)}
        queue: deque[tuple[Version, list[Migration]]] = deque([(start, [])])
        while queue:
            current, path = queue.popleft()
            if len(path) >= max_steps:
                continue
            for edge in edges:
                if not edge.from_range.contains(current):
                    continue
                nxt = edge.to_version
                if str(nxt) in seen:
                    continue
                new_path = path + [edge]
                if target.contains(nxt):
                    return new_path
                seen.add(str(nxt))
                queue.append((nxt, new_path))
        return None

    def apply(
        self, plan: Iterable[Migration], data: Any, context: Any = None
    ) -> tuple[Any, list[str]]:
        applied: list[str] = []
        for mig in plan:
            data = mig.apply(data, context)
            applied.append(mig.label())
        return data, applied

    def all(self) -> list[Migration]:
        return list(self._edges)

    def clear(self) -> None:
        self._edges.clear()
