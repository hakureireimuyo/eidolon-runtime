"""资源空间：一个已加载工程包在运行时的全部资源视图。

`ResourceSpace` 取代了过去"引擎只认识一个 Character"的硬编码结构。它是一张按
id 与类型标签双索引的资源表：

    space = load_package("world.cart")
    space.first("application/x-eidolon-character")   # 有就用，没有也不报错
    space.of_type("application/x-eidolon-*")         # 按通配拿一族资源
    space.create("application/x-eidolon-quest", {...})  # 运行时动态新增
    space.save("world.cart")                          # 连未知数据一起写回

写回时的关键保证：**没有 handler 能解释的资源，用原始字节原样写回**，所以低版本
运行时打开高版本工程再保存，也不会丢掉自己看不懂的那部分数据。
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Iterable, Iterator, Optional

from . import typespec
from .dynamic import unwrap
from .model import (
    KIND_MEDIA,
    KIND_VIRTUAL,
    STATUS_LOADED,
    LoadContext,
    ResourceDescriptor,
    ResourceRecord,
)
from .registry import ResourceRegistry, registry as default_registry
from .router import ResourceRouter


class ResourceSpace:
    """一个包的运行时资源集合。"""

    def __init__(
        self,
        *,
        manifest: Optional[dict] = None,
        registry: Optional[ResourceRegistry] = None,
        router: Optional[ResourceRouter] = None,
        source: str = "",
    ) -> None:
        self.manifest: dict = dict(manifest or {})
        self.registry = registry or default_registry
        self.router = router or ResourceRouter(self.registry)
        self.source = source
        self.records: dict[str, ResourceRecord] = {}
        self.media: dict[str, bytes] = {}
        self.media_types: dict[str, str] = {}
        self.media_paths: dict[str, str] = {}
        self.context = LoadContext(
            manifest=self.manifest,
            media=self.media,
            media_types=self.media_types,
            registry=self.registry,
            router=self.router,
            space=self,
        )

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.manifest.get("id", "")

    @property
    def name(self) -> str:
        return self.manifest.get("name", "")

    @property
    def container_version(self) -> Any:
        return self.manifest.get("version")

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    def ingest(self, descriptor: ResourceDescriptor) -> ResourceRecord:
        """路由并装载一份资源描述符。"""
        record = self.router.load(descriptor, self.context)
        self.records[record.id] = record
        return record

    def ingest_all(self, descriptors: Iterable[ResourceDescriptor]) -> list[ResourceRecord]:
        return [self.ingest(d) for d in descriptors]

    def add_media(self, resource_id: str, data: bytes, mime: str = "", path: str = "") -> None:
        self.media[resource_id] = data
        self.media_types[resource_id] = mime or typespec.guess_type(path or resource_id)
        if path:
            self.media_paths[resource_id] = path

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, resource_id: str) -> Optional[ResourceRecord]:
        return self.records.get(resource_id)

    def value(self, resource_id: str, default: Any = None) -> Any:
        record = self.records.get(resource_id)
        return default if record is None else record.value

    def of_type(
        self,
        pattern: str,
        *,
        usable_only: bool = False,
        typed_only: bool = False,
    ) -> list[ResourceRecord]:
        """按类型标签（支持通配）取一族资源，按特异度与 id 稳定排序。

        `typed_only=True` 只返回被专用 handler 解释过的记录（取角色卡这类
        领域对象时用），默认连通用装载的未知资源一起返回。
        """
        matched = []
        for record in self.records.values():
            score = typespec.match_score(pattern, record.type)
            if score is None:
                continue
            if typed_only and not record.is_typed:
                continue
            if usable_only and not record.is_usable:
                continue
            matched.append((score, record.id, record))
        matched.sort(key=lambda item: (-item[0], item[1]))
        return [record for _, _, record in matched]

    def first(
        self,
        pattern: str,
        *,
        usable_only: bool = False,
        typed_only: bool = False,
    ) -> Optional[ResourceRecord]:
        found = self.of_type(pattern, usable_only=usable_only, typed_only=typed_only)
        return found[0] if found else None

    def first_value(
        self,
        pattern: str,
        default: Any = None,
        *,
        usable_only: bool = True,
        typed_only: bool = False,
    ) -> Any:
        record = self.first(pattern, usable_only=usable_only, typed_only=typed_only)
        return default if record is None else record.value

    def types(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records.values():
            counts[record.type] = counts.get(record.type, 0) + 1
        return counts

    def filter(self, predicate: Callable[[ResourceRecord], bool]) -> list[ResourceRecord]:
        return [r for r in self.records.values() if predicate(r)]

    def __iter__(self) -> Iterator[ResourceRecord]:
        return iter(self.records.values())

    def __len__(self) -> int:
        return len(self.records)

    def __contains__(self, resource_id: object) -> bool:
        return resource_id in self.records

    # ------------------------------------------------------------------
    # 动态创建 / 修改
    # ------------------------------------------------------------------
    def create(
        self,
        type_value: str,
        data: Any = None,
        *,
        id: Optional[str] = None,
        version: Optional[str] = None,
        path: Optional[str] = None,
        required: bool = False,
    ) -> ResourceRecord:
        """在运行时新增一份资源。

        类型未注册也能创建——会走通用 JSON 处理，字节照样能写回包。
        """
        resource_id = id or _default_id(type_value, self.records)
        found = self.registry.resolve(type_value)
        handler = found[0] if found else None
        if version is None and handler is not None:
            version = handler.default_version()
        descriptor = ResourceDescriptor(
            id=resource_id,
            type=type_value,
            path=path or f"data/{resource_id}.json",
            version=version,
            kind=KIND_VIRTUAL,
            required=required,
            declared=False,
        )
        payload = unwrap(data) if data is not None else {}
        raw = b""
        if handler is not None:
            try:
                raw = handler.encode(payload, descriptor)
            except Exception:  # noqa: BLE001
                raw = b""
        if not raw:
            from .router import _fallback_encode

            raw = _fallback_encode(payload)
        descriptor.reader = lambda data=raw: data
        descriptor.size = len(raw)
        record = self.ingest(descriptor)
        if record.status == STATUS_LOADED:
            record.note("运行时动态创建")
        return record

    def remove(self, resource_id: str) -> bool:
        return self.records.pop(resource_id, None) is not None

    def replace(self, resource_id: str, value: Any) -> Optional[ResourceRecord]:
        record = self.records.get(resource_id)
        if record is None:
            return None
        record.value = value
        record.raw = None  # 强制重新编码
        return record

    # ------------------------------------------------------------------
    # 报告 / 写回
    # ------------------------------------------------------------------
    def report(self, *, include_values: bool = False) -> dict:
        records = [
            r.to_dict(include_value=include_values)
            for r in sorted(self.records.values(), key=lambda r: r.id)
        ]
        return {
            "package": {
                "id": self.id,
                "name": self.name,
                "container_version": self.container_version,
                "source": self.source,
            },
            "counts": {
                "total": len(self.records),
                "usable": sum(1 for r in self.records.values() if r.is_usable),
                "typed": sum(1 for r in self.records.values() if r.is_typed),
                "media": len(self.media),
                "by_status": _tally(r.status for r in self.records.values()),
                "by_kind": _tally(r.kind for r in self.records.values()),
            },
            "types": self.types(),
            "resources": records,
            "media": [
                {
                    "id": rid,
                    "type": self.media_types.get(rid, ""),
                    "size": len(data),
                    "path": self.media_paths.get(rid, ""),
                }
                for rid, data in sorted(self.media.items())
            ],
        }

    def to_package(self, package: Any = None) -> Any:
        """把资源空间写回一个 Cartridge 包对象（未知数据用原始字节保留）。"""
        import cartridge as cart

        if package is None:
            package = cart.create_package(
                self.name or "untitled",
                id=self.id or None,
                author=self.manifest.get("author"),
                description=self.manifest.get("description"),
            )
        for record in sorted(self.records.values(), key=lambda r: r.id):
            if record.kind == KIND_MEDIA:
                continue
            data = record.raw if record.raw is not None else self.router.encode(record)
            package.add_entry(
                record.id,
                record.type,
                data,
                path=record.path or None,
                version=record.effective_version or record.source_version,
                required=record.descriptor.required,
            )
        for rid, data in self.media.items():
            package.add_resource(
                rid,
                self.media_types.get(rid, typespec.DEFAULT_TYPE),
                data,
                path=self.media_paths.get(rid) or None,
            )
        return package

    def save(self, output_path: str, *, image_source: Optional[str] = None) -> str:
        """写出 .cart（或提供封面图时写出 .png）。"""
        import cartridge as cart

        package = self.to_package()
        if image_source:
            cart.write(package, image_source, output_path)
        else:
            cart.write_cart(package, output_path)
        return output_path

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ResourceSpace {self.name or self.id!r} resources={len(self.records)}>"


def _tally(values: Iterable[str]) -> dict:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def _default_id(type_value: str, existing: dict) -> str:
    _, subtype, _ = typespec.split(type_value)
    base = (subtype or "resource").replace("x-eidolon-", "").strip("-") or "resource"
    if base not in existing:
        return base
    for i in range(2, 1000):
        candidate = f"{base}-{i}"
        if candidate not in existing:
            return candidate
    return f"{base}-{uuid.uuid4().hex[:8]}"
