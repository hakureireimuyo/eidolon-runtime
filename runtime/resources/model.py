"""资源路由的数据模型：描述符、加载记录与加载上下文。

`ResourceDescriptor` 是**寻址单元**——路由器只看描述符（id / type / version /
path），不关心它来自 manifest 声明的数据块、包内未声明的孤儿文件，还是运行时
凭空创建的内容。三者一视同仁，这是"自动适配"的前提。

`ResourceRecord` 是**加载结果**——即便加载失败或版本不匹配，也一定会产出记录，
只是状态不同。整包加载永远不会因为某一份数据而中断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional  # noqa: F401

# 描述符来源
KIND_ENTRY = "entry"      # manifest.entries 声明的数据块
KIND_FILE = "file"        # 包内存在但 manifest 未声明（自动发现）
KIND_MEDIA = "media"      # manifest.resources 声明的媒体字节
KIND_VIRTUAL = "virtual"  # 运行时动态创建，尚未写回包

# 加载状态
STATUS_LOADED = "loaded"        # 类型与版本都命中，完整加载
STATUS_MIGRATED = "migrated"    # 经迁移链升级后加载
STATUS_FORWARD = "forward"      # 数据比 handler 新，按前向兼容宽容加载
STATUS_DEGRADED = "degraded"    # 版本或解析不匹配，退化为通用数据
STATUS_GENERIC = "generic"      # 无专用 handler，按通用类型加载
STATUS_ERROR = "error"          # 连字节都取不到

# 值可用（拿得到有意义的数据）
USABLE_STATUSES = {STATUS_LOADED, STATUS_MIGRATED, STATUS_FORWARD, STATUS_GENERIC}
# 被专用 handler 赋予了领域语义
TYPED_STATUSES = {STATUS_LOADED, STATUS_MIGRATED, STATUS_FORWARD}


@dataclass
class ResourceDescriptor:
    """一份资源的寻址信息（字节惰性读取）。"""

    id: str
    type: str
    path: str = ""
    version: Optional[str] = None
    kind: str = KIND_ENTRY
    required: bool = False
    declared: bool = True
    size: int = 0
    meta: dict = field(default_factory=dict)
    reader: Optional[Callable[[], bytes]] = None
    _cache: Optional[bytes] = field(default=None, repr=False, compare=False)

    def read(self) -> bytes:
        """取回原始字节（首次调用后缓存）。"""
        if self._cache is not None:
            return self._cache
        data = b"" if self.reader is None else (self.reader() or b"")
        self._cache = data
        if not self.size:
            self.size = len(data)
        return data

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "path": self.path,
            "version": self.version,
            "kind": self.kind,
            "required": self.required,
            "declared": self.declared,
            "size": self.size,
            "meta": dict(self.meta),
        }


@dataclass
class LoadContext:
    """加载期共享上下文，handler 可借此访问同包的其它资源。"""

    manifest: dict = field(default_factory=dict)
    media: dict = field(default_factory=dict)        # {资源 id: bytes}
    media_types: dict = field(default_factory=dict)  # {资源 id: mime}
    registry: Any = None
    router: Any = None
    space: Any = None
    extras: dict = field(default_factory=dict)

    def media_bytes(self, resource_id: str) -> Optional[bytes]:
        return self.media.get(resource_id)


@dataclass
class ResourceRecord:
    """一份资源的加载结果。"""

    descriptor: ResourceDescriptor
    value: Any = None
    status: str = STATUS_LOADED
    handler: Optional[str] = None
    route_score: Optional[int] = None
    source_version: Optional[str] = None
    effective_version: Optional[str] = None
    migrations: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    generic: bool = False
    raw: Optional[bytes] = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.descriptor.id

    @property
    def type(self) -> str:
        return self.descriptor.type

    @property
    def kind(self) -> str:
        return self.descriptor.kind

    @property
    def path(self) -> str:
        return self.descriptor.path

    @property
    def is_usable(self) -> bool:
        """值是否可用（含无领域语义的通用装载；只有 degraded / error 不算）。"""
        return self.status in USABLE_STATUSES

    @property
    def is_typed(self) -> bool:
        """是否被**专用** handler 赋予了领域语义（通用装载不算）。"""
        return (not self.generic) and self.status in TYPED_STATUSES

    def note(self, message: str) -> "ResourceRecord":
        self.diagnostics.append(message)
        return self

    def to_dict(self, *, include_value: bool = False) -> dict:
        out = {
            **self.descriptor.to_dict(),
            "status": self.status,
            "handler": self.handler,
            "route_score": self.route_score,
            "source_version": self.source_version,
            "effective_version": self.effective_version,
            "migrations": list(self.migrations),
            "diagnostics": list(self.diagnostics),
            "usable": self.is_usable,
            "typed": self.is_typed,
            "generic": self.generic,
        }
        if include_value:
            out["value"] = describe_value(self.value)
        return out

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<ResourceRecord {self.id} type={self.type} "
            f"status={self.status} handler={self.handler}>"
        )


def describe_value(value: Any) -> Any:
    """把任意 handler 产物转成可 JSON 序列化的视图（供 API / 报告使用）。"""
    from dataclasses import asdict, is_dataclass

    from .dynamic import DynamicResource

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, DynamicResource):
        return value.to_dict()
    if isinstance(value, bytes):
        return {"__bytes__": len(value)}
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return asdict(value)
        except Exception:  # noqa: BLE001 - 含不可序列化字段时退化
            return repr(value)
    if isinstance(value, dict):
        return {str(k): describe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [describe_value(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:  # noqa: BLE001
            return repr(value)
    return repr(value)
