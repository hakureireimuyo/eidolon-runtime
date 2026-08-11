"""资源路由器：描述符进，加载记录出。

路由 = **按类型标签选 handler** + **按版本决定兼容策略** + **失败逐级降级**。

加载流水线（任何一步出问题都只降级、不抛错）：

    字节 -> [选 handler] -> [decode] -> [版本裁决] -> [load] -> ResourceRecord
                 ↓失败          ↓失败        ↓无迁移路径    ↓失败
              通用兜底      原始字节      前向/降级加载   通用动态资源

这条"永不中断"的规则很关键：一个包里有 20 份数据，其中 1 份来自未来版本的
未知模块，运行时依旧要能把另外 19 份跑起来，并把第 20 份原样保留（写回不丢）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .dynamic import wrap
from .handler import ResourceHandler
from .model import (
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_FORWARD,
    STATUS_GENERIC,
    STATUS_LOADED,
    STATUS_MIGRATED,
    LoadContext,
    ResourceDescriptor,
    ResourceRecord,
)
from .registry import ResourceRegistry, registry as default_registry
from .versioning import Version, VersionRange


@dataclass
class RouteDecision:
    """一次路由裁决的结果。"""

    handler: Optional[ResourceHandler]
    score: Optional[int]
    reason: str

    @property
    def is_specific(self) -> bool:
        return self.handler is not None and (self.score or 0) > 0


class ResourceRouter:
    """无状态路由器：可安全并发复用同一实例。"""

    def __init__(self, reg: Optional[ResourceRegistry] = None) -> None:
        self.registry = reg or default_registry

    # ------------------------------------------------------------------
    def route(self, descriptor: ResourceDescriptor) -> RouteDecision:
        found = self.registry.resolve(descriptor.type)
        if found is None:
            return RouteDecision(None, None, f"没有匹配 {descriptor.type!r} 的处理器")
        handler, score = found
        if score == 0:
            return RouteDecision(handler, score, "仅命中全局兜底处理器")
        return RouteDecision(handler, score, f"命中 {handler.name}（特异度 {score}）")

    # ------------------------------------------------------------------
    def load(
        self,
        descriptor: ResourceDescriptor,
        context: Optional[LoadContext] = None,
    ) -> ResourceRecord:
        """加载一份资源，任何情况下都返回记录。"""
        context = context or LoadContext()
        record = ResourceRecord(
            descriptor=descriptor,
            source_version=descriptor.version,
            effective_version=descriptor.version,
        )

        # 1) 取字节
        try:
            raw = descriptor.read()
        except Exception as exc:  # noqa: BLE001
            record.status = STATUS_ERROR
            record.note(f"读取字节失败：{exc}")
            return record
        record.raw = raw

        # 2) 选 handler
        decision = self.route(descriptor)
        handler = decision.handler
        if handler is None:
            record.status = STATUS_GENERIC
            record.value = _generic_value(raw)
            record.note(decision.reason)
            return record
        record.handler = handler.name
        record.route_score = decision.score
        record.generic = bool(getattr(handler, "generic", False))

        # 3) 解码
        try:
            data = handler.decode(raw, descriptor)
        except Exception as exc:  # noqa: BLE001
            record.status = STATUS_DEGRADED
            record.value = _generic_value(raw)
            record.note(f"{handler.name} 解码失败，退回原始数据：{exc}")
            return record

        # 4) 版本裁决
        status = (
            STATUS_GENERIC
            if (record.generic or not decision.is_specific)
            else STATUS_LOADED
        )
        supported = VersionRange.parse(handler.versions)
        source = Version.parse(descriptor.version)
        if descriptor.version and not supported.contains(source):
            plan = self.registry.migrations.plan(descriptor.type, source, supported)
            if plan:
                try:
                    data, applied = self.registry.migrations.apply(plan, data, context)
                    record.migrations = applied
                    record.effective_version = str(plan[-1].to_version)
                    status = STATUS_MIGRATED
                except Exception as exc:  # noqa: BLE001
                    record.note(f"迁移链执行失败：{exc}")
                    status = STATUS_DEGRADED
            else:
                upper = supported.upper
                if upper is not None and source > upper and handler.forward_compatible:
                    status = STATUS_FORWARD
                    record.note(
                        f"数据版本 {source} 高于 {handler.name} 支持的 {supported}，"
                        "按前向兼容加载（未知字段原样保留）"
                    )
                else:
                    status = STATUS_DEGRADED
                    record.note(
                        f"数据版本 {source} 不在 {handler.name} 支持的 {supported} 内，"
                        "且无迁移路径"
                    )

        # 5) 交给 handler 解释；失败则退回通用动态资源
        try:
            record.value = handler.load(data, descriptor, context)
            record.status = status
        except Exception as exc:  # noqa: BLE001
            record.status = STATUS_DEGRADED
            record.value = wrap(data) if isinstance(data, (dict, list)) else data
            record.note(f"{handler.name} 解释失败，退回通用数据：{exc}")
        return record

    # ------------------------------------------------------------------
    def encode(self, record: ResourceRecord) -> bytes:
        """把记录序列化回字节（写回包 / 导出时使用）。"""
        found = self.registry.resolve(record.type)
        if found is not None:
            handler, _ = found
            try:
                return handler.encode(record.value, record.descriptor)
            except Exception:  # noqa: BLE001 - 编码失败时退回原始字节
                pass
        if record.raw is not None:
            return record.raw
        return _fallback_encode(record.value)


def _generic_value(raw: bytes) -> Any:
    """无 handler 可用时的通用值：能解析成 JSON 就给动态资源，否则给字节。"""
    import json

    try:
        text = bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return wrap(json.loads(stripped))
        except ValueError:
            return text
    return text


def _fallback_encode(value: Any) -> bytes:
    import json

    from .dynamic import unwrap

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return json.dumps(unwrap(value), ensure_ascii=False, indent=2).encode("utf-8")
    except (TypeError, ValueError):
        return repr(value).encode("utf-8")


# 默认路由器（绑定全局注册表）
router = ResourceRouter()
