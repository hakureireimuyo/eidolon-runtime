"""动态资源：没有预定义 Schema 也能访问的数据对象。

这是"不需要提前定义工程资源内容"的核心。任何未注册类型的 JSON 数据都会被包成
`DynamicResource`：

    world = space.first("application/x-eidolon-world").value
    world.geography.regions[0].name        # 属性式访问
    world["factions.0.leader"]             # 点路径访问
    world.get("economy.currency", "金币")   # 带默认值
    world.schema()                         # 推断出的结构，供 UI 自动生成表单

三条硬约束：

- **零丢失**：原始数据完整保留，未知字段不会在读写往返中被抹掉——这是跨版本
  兼容的前提（老运行时读新数据，写回时新字段仍在）。
- **零异常**：访问不存在的路径返回 `None` 而不是抛错，避免一个字段缺失就中断
  整条加载链路。
- **零依赖**：只用标准库，可被任何层复用。
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

_MISSING = object()


def wrap(value: Any) -> Any:
    """把普通 JSON 结构递归包装为可导航对象（标量原样返回）。"""
    if isinstance(value, DynamicResource):
        return value
    if isinstance(value, dict):
        return DynamicResource(value)
    if isinstance(value, (list, tuple)):
        return [wrap(v) for v in value]
    return value


def unwrap(value: Any) -> Any:
    """还原为纯 JSON 结构（写回时使用）。"""
    if isinstance(value, DynamicResource):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value]
    return value


def infer_schema(value: Any, _depth: int = 0) -> Any:
    """从数据推断结构描述（供编辑器动态生成表单 / 校验）。"""
    if _depth > 6:
        return "any"
    if isinstance(value, DynamicResource):
        value = value.to_dict()
    if isinstance(value, dict):
        return {k: infer_schema(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return ["any"]
        return [infer_schema(value[0], _depth + 1)]
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


class DynamicResource:
    """字典的自适应视图：属性访问、点路径查询、结构推断，且永不丢字段。"""

    __slots__ = ("_data",)

    def __init__(self, data: Optional[dict] = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))

    # ---- 读 ----
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        data = object.__getattribute__(self, "_data")
        if name in data:
            return wrap(data[name])
        return None

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and ("." in key or key not in self._data):
            return self.get(key)
        return wrap(self._data.get(key))

    def get(self, path: str, default: Any = None) -> Any:
        """按点路径取值，支持列表下标：`a.b.0.c`。"""
        current: Any = self._data
        for part in str(path).split("."):
            if isinstance(current, DynamicResource):
                current = current.to_dict()
            if isinstance(current, dict):
                if part not in current:
                    return default
                current = current[part]
            elif isinstance(current, (list, tuple)):
                if not part.lstrip("-").isdigit():
                    return default
                index = int(part)
                if not -len(current) <= index < len(current):
                    return default
                current = current[index]
            else:
                return default
        return wrap(current)

    def has(self, path: str) -> bool:
        return self.get(path, _MISSING) is not _MISSING

    # ---- 写（动态创建 / 就地演化） ----
    def set(self, path: str, value: Any) -> "DynamicResource":
        """按点路径写值，缺失的中间层会自动创建。"""
        parts = str(path).split(".")
        current = self._data
        for part in parts[:-1]:
            nxt = current.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                current[part] = nxt
            current = nxt
        current[parts[-1]] = unwrap(value)
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__slots__:
            object.__setattr__(self, name, value)
        else:
            self._data[name] = unwrap(value)

    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(key, str) and "." in key:
            self.set(key, value)
        else:
            self._data[key] = unwrap(value)

    def merge(self, other: Any, *, overwrite: bool = True) -> "DynamicResource":
        """深合并另一份数据（用于迁移 / 打补丁，未提及的字段保持不变）。"""
        patch = unwrap(other) or {}

        def _merge(dst: dict, src: dict) -> dict:
            for k, v in src.items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _merge(dst[k], v)
                elif overwrite or k not in dst:
                    dst[k] = v
            return dst

        _merge(self._data, patch)
        return self

    # ---- 容器协议 ----
    def keys(self):
        return self._data.keys()

    def items(self):
        return ((k, wrap(v)) for k, v in self._data.items())

    def values(self):
        return (wrap(v) for v in self._data.values())

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str) and "." in key:
            return self.has(key)
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        return bool(self._data)

    def __eq__(self, other: Any) -> bool:
        return unwrap(self) == unwrap(other)

    # ---- 导出 ----
    def to_dict(self) -> dict:
        return dict(self._data)

    def schema(self) -> dict:
        return infer_schema(self._data)

    def __repr__(self) -> str:
        preview = ", ".join(list(self._data)[:6])
        more = "…" if len(self._data) > 6 else ""
        return f"<DynamicResource {{{preview}{more}}}>"
