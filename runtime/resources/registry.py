"""资源注册表:类型标签 -> 处理器 的可扩展映射。

内核对"有哪些资源类型"完全无知；一切认知都来自注册表。三种注册方式:

1. **写代码**——`@registry.handler("application/x-eidolon-world", versions="^1.0")`
   装饰一个函数或 handler 类。
2. **写声明**——`registry.define("application/x-eidolon-quest", required=("title",))`
   运行时凭空定义一个新类型,不需要任何 Python 类。
3. **装插件**——`registry.discover()` 扫描 `eidolon.resources` entry point 或
   直接 `registry.install(module)`,第三方包即插即用。

配套的迁移注册(`@registry.migration`)让同一类型的历史版本能自动升级到当前
handler 支持的范围内。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Iterable, Optional, Sequence

from . import typespec
from .handler import (
    FunctionHandler,
    RawHandler,
    ResourceHandler,
    SchemaHandler,
)
from .versioning import MigrationGraph


class ResourceRegistry:
    """处理器与迁移边的容器。可创建多个实例以实现隔离(测试 / 沙箱)。"""

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._handlers: list[tuple[int, ResourceHandler]] = []
        self._seq = 0
        self._cache: dict[str, Optional[tuple[ResourceHandler, int]]] = {}
        self.migrations = MigrationGraph()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, handler: ResourceHandler) -> ResourceHandler:
        """注册一个 handler 实例。同特异度时后注册者优先(便于覆盖内置行为)。"""
        if not isinstance(handler, ResourceHandler):
            raise TypeError(f"handler 必须是 ResourceHandler,收到 {type(handler)!r}")
        self._seq += 1
        self._handlers.append((self._seq, handler))
        self._cache.clear()
        return handler

    def unregister(self, name: str) -> bool:
        before = len(self._handlers)
        self._handlers = [(s, h) for s, h in self._handlers if h.name != name]
        self._cache.clear()
        return len(self._handlers) != before

    def handler(
        self,
        *type_patterns: str,
        versions: str = "*",
        name: Optional[str] = None,
        description: str = "",
        decoder: Optional[Callable[..., Any]] = None,
        encoder: Optional[Callable[..., Any]] = None,
    ):
        """装饰器:把函数或 handler 类注册为处理器。

            @registry.handler("application/x-eidolon-world", versions="^1.0")
            def load_world(data, descriptor, context):
                return World(**data)
        """

        def decorate(target):
            if inspect.isclass(target) and issubclass(target, ResourceHandler):
                instance = target()
                if type_patterns:
                    instance.type_patterns = tuple(type_patterns)
                if versions != "*":
                    instance.versions = versions
                if name:
                    instance.name = name
                if description:
                    instance.description = description
                self.register(instance)
                return target
            if not callable(target):
                raise TypeError("registry.handler 只能装饰函数或 ResourceHandler 子类")
            self.register(
                FunctionHandler(
                    target,
                    type_patterns=type_patterns or ("*/*",),
                    versions=versions,
                    name=name,
                    description=description,
                    decoder=decoder,
                    encoder=encoder,
                )
            )
            return target

        return decorate

    def define(
        self,
        type_value: str,
        *,
        version: str = "1.0",
        versions: Optional[str] = None,
        required: Sequence[str] = (),
        defaults: Optional[dict] = None,
        schema: Optional[dict] = None,
        description: str = "",
        strict: bool = False,
    ) -> SchemaHandler:
        """运行时动态定义一个资源类型(无需编写 handler 类)。"""
        handler = SchemaHandler(
            type_value,
            version=version,
            versions=versions,
            required=required,
            defaults=defaults,
            schema=schema,
            description=description,
            strict=strict,
        )
        self.register(handler)
        return handler

    def migration(
        self,
        type_pattern: str,
        *,
        frm: Any,
        to: Any,
        note: str = "",
    ):
        """装饰器:注册一条版本迁移边。

            @registry.migration("application/x-eidolon-world", frm="<1.0", to="1.0")
            def upgrade(data, context):
                data["regions"] = data.pop("areas", [])
                return data
        """

        def decorate(fn):
            self.migrations.add(type_pattern, frm, to, fn, note)
            return fn

        return decorate

    def add_migration(
        self, type_pattern: str, frm: Any, to: Any, fn: Callable, note: str = ""
    ):
        return self.migrations.add(type_pattern, frm, to, fn, note)

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    def resolve(self, type_value: str) -> Optional[tuple[ResourceHandler, int]]:
        """按特异度选出最合适的 handler,返回 (handler, 分数)。"""
        key = typespec.normalize(type_value)
        if key in self._cache:
            return self._cache[key]
        best: Optional[tuple[ResourceHandler, int]] = None
        best_rank: tuple[int, int] = (-1, -1)
        for seq, handler in self._handlers:
            for pattern in handler.type_patterns:
                score = typespec.match_score(pattern, key)
                if score is None:
                    continue
                rank = (score, seq)  # 同分时后注册者胜出
                if rank > best_rank:
                    best_rank = rank
                    best = (handler, score)
        self._cache[key] = best
        return best

    def supports(self, type_value: str) -> bool:
        """是否存在**专用**(非全局兜底)handler。"""
        found = self.resolve(type_value)
        return bool(found and found[1] > 0)

    # ------------------------------------------------------------------
    # 插件发现
    # ------------------------------------------------------------------
    def install(self, target: Any) -> list[str]:
        """安装一个扩展:模块(调用其 `register(registry)`)或可调用对象。"""
        installed: list[str] = []
        hook = getattr(target, "register", target)
        if callable(hook):
            hook(self)
            installed.append(getattr(target, "__name__", repr(target)))
        return installed

    def discover(self, group: str = "eidolon.resources") -> list[str]:
        """从已安装发行包的 entry point 组自动装载扩展。"""
        loaded: list[str] = []
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover - Python < 3.8
            return loaded
        try:
            eps: Iterable = entry_points(group=group)
        except TypeError:  # pragma: no cover - 旧版 API
            eps = entry_points().get(group, [])  # type: ignore[attr-defined]
        for ep in eps:
            try:
                self.install(ep.load())
                loaded.append(ep.name)
            except Exception:  # noqa: BLE001 - 单个插件失败不影响其它
                continue
        return loaded

    # ------------------------------------------------------------------
    # 自省 / 隔离
    # ------------------------------------------------------------------
    def handlers(self) -> list[ResourceHandler]:
        return [h for _, h in sorted(self._handlers, key=lambda x: x[0])]

    def report(self) -> dict:
        return {
            "registry": self.name,
            "handlers": [h.to_dict() for h in self.handlers()],
            "migrations": [
                {
                    "type": m.type_pattern,
                    "from": str(m.from_range),
                    "to": str(m.to_version),
                    "note": m.note,
                }
                for m in self.migrations.all()
            ],
        }

    def clone(self, name: Optional[str] = None) -> "ResourceRegistry":
        """复制一份(测试中隔离改动,不污染全局注册表)。"""
        clone = ResourceRegistry(name or f"{self.name}-clone")
        clone._handlers = list(self._handlers)
        clone._seq = self._seq
        for m in self.migrations.all():
            clone.migrations.add(
                m.type_pattern, m.from_range, m.to_version, m.fn, m.note
            )
        return clone

    def clear(self) -> None:
        self._handlers.clear()
        self._cache.clear()
        self.migrations.clear()
        self._seq = 0


# 全局默认注册表(内置 handler 在 builtin.install_builtins 中装入)。
registry = ResourceRegistry("default")


def ensure_fallback(target: ResourceRegistry) -> None:
    """保证注册表至少有一个全局兜底 handler。"""
    if target.resolve("application/octet-stream") is None:
        target.register(RawHandler())
