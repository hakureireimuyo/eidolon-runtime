"""包加载入口：把任意 Cartridge 包摊平成资源描述符，再交给路由器装载。

**不需要提前定义工程包里有什么**——描述符有三个来源，运行时一视同仁：

1. `manifest.entries` 声明的数据块（正规来源）；
2. 包内**存在但未声明**的文件（孤儿文件，按扩展名推断类型后自动纳入）；
3. `manifest.resources` 声明的媒体字节（进 `space.media`，语义归扩展层）。

对包对象只做鸭子类型假设（`manifest` / `entries` / `raw_files`），不硬依赖
cartridge；传入文件路径时才按需 import，方便测试用桩对象。
"""

from __future__ import annotations

import posixpath
from typing import Any, Optional

from . import typespec
from .model import KIND_ENTRY, KIND_FILE, ResourceDescriptor
from .registry import ResourceRegistry, registry as default_registry
from .router import ResourceRouter
from .space import ResourceSpace

MANIFEST_NAMES = {"manifest.json", "/manifest.json"}


def open_package(path: str) -> Any:
    """打开 .cart / .png / .zip，返回 Cartridge Package。"""
    import cartridge as cart

    return cart.open(path)


def _entry_descriptors(package: Any) -> tuple[list[ResourceDescriptor], list[dict], set[str]]:
    """从 manifest.entries + package.entries 生成描述符。"""
    manifest = getattr(package, "manifest", {}) or {}
    entries = getattr(package, "entries", {}) or {}
    raw_files = getattr(package, "raw_files", {}) or {}

    descriptors: list[ResourceDescriptor] = []
    missing: list[dict] = []
    claimed: set[str] = set()

    declared = manifest.get("entries") or []
    seen_ids: set[str] = set()

    for decl in declared:
        entry_id = decl.get("id")
        path = decl.get("path") or ""
        if path:
            claimed.add(path)
        entry = entries.get(entry_id) if entry_id else None
        data = None
        if entry is not None:
            data = getattr(entry, "data", None)
        if data is None and path in raw_files:
            data = raw_files[path]
        if data is None:
            missing.append(
                {
                    "id": entry_id,
                    "path": path,
                    "type": decl.get("type", ""),
                    "required": bool(decl.get("required")),
                }
            )
            continue
        seen_ids.add(entry_id)
        descriptors.append(
            ResourceDescriptor(
                id=entry_id,
                type=typespec.normalize(decl.get("type")) or typespec.guess_type(path),
                path=path,
                version=_as_version_text(decl.get("version")),
                kind=KIND_ENTRY,
                required=bool(decl.get("required")),
                declared=True,
                size=len(data),
                reader=(lambda d=data: d),
            )
        )

    # manifest 未声明、但 Package.entries 里存在的数据块（构建期直接塞入的情况）
    for entry_id, entry in entries.items():
        if entry_id in seen_ids:
            continue
        data = getattr(entry, "data", None)
        if data is None:
            continue
        path = getattr(entry, "path", "") or ""
        if path:
            claimed.add(path)
        descriptors.append(
            ResourceDescriptor(
                id=entry_id,
                type=typespec.normalize(getattr(entry, "type", ""))
                or typespec.guess_type(path),
                path=path,
                version=_as_version_text(getattr(entry, "version", None)),
                kind=KIND_ENTRY,
                required=bool(getattr(entry, "required", False)),
                declared=False,
                size=len(data),
                reader=(lambda d=data: d),
            )
        )

    return descriptors, missing, claimed


def _orphan_descriptors(
    package: Any, claimed: set[str], existing_ids: set[str]
) -> list[ResourceDescriptor]:
    """扫描 manifest 未声明的包内文件，按扩展名推断类型自动纳入。"""
    raw_files = getattr(package, "raw_files", {}) or {}
    out: list[ResourceDescriptor] = []
    for path, data in sorted(raw_files.items()):
        normalized = path.lstrip("./")
        if normalized in MANIFEST_NAMES or path in claimed or normalized in claimed:
            continue
        if path.endswith("/"):
            continue
        resource_id = _id_from_path(path, existing_ids)
        existing_ids.add(resource_id)
        out.append(
            ResourceDescriptor(
                id=resource_id,
                type=typespec.guess_type(path),
                path=path,
                version=None,
                kind=KIND_FILE,
                required=False,
                declared=False,
                size=len(data),
                meta={"discovered": True},
                reader=(lambda d=data: d),
            )
        )
    return out


def descriptors_from_package(
    package: Any, *, include_undeclared: bool = True
) -> tuple[list[ResourceDescriptor], list[dict]]:
    """把一个包摊平成描述符列表，附带缺失数据块清单。"""
    descriptors, missing, claimed = _entry_descriptors(package)
    manifest = getattr(package, "manifest", {}) or {}
    for res in manifest.get("resources") or []:
        if res.get("path"):
            claimed.add(res["path"])
    if include_undeclared:
        existing_ids = {d.id for d in descriptors}
        descriptors.extend(_orphan_descriptors(package, claimed, existing_ids))
    return descriptors, missing


def load_package(
    source: Any,
    *,
    registry: Optional[ResourceRegistry] = None,
    router: Optional[ResourceRouter] = None,
    include_undeclared: bool = True,
) -> ResourceSpace:
    """加载一个工程包（路径或已打开的 Package），返回资源空间。

    绝不因为某份数据无法解释而失败——未知类型进 `generic`，坏版本进 `degraded`，
    缺失数据块进报告的 `missing` 列表。
    """
    reg = registry or default_registry
    _ensure_builtins(reg)

    if isinstance(source, (str, bytes)) or hasattr(source, "__fspath__"):
        path = str(source)
        package = open_package(path)
    else:
        path = getattr(source, "source_path", "")
        package = source

    manifest = getattr(package, "manifest", {}) or {}
    space = ResourceSpace(
        manifest=manifest,
        registry=reg,
        router=router or ResourceRouter(reg),
        source=path,
    )

    # 媒体字节：语义归扩展层，容器只负责寻址
    raw_files = getattr(package, "raw_files", {}) or {}
    for res in manifest.get("resources") or []:
        rid = res.get("id")
        rpath = res.get("path") or ""
        data = raw_files.get(rpath)
        if rid is None or data is None:
            continue
        space.add_media(rid, data, res.get("type") or typespec.guess_type(rpath), rpath)

    descriptors, missing = descriptors_from_package(
        package, include_undeclared=include_undeclared
    )
    space.ingest_all(descriptors)
    space.context.extras["missing"] = missing
    space.context.extras["package"] = package
    return space


def _as_version_text(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return str(value)


def _id_from_path(path: str, existing: set[str]) -> str:
    base = posixpath.basename(path)
    stem = base.rsplit(".", 1)[0] if "." in base else base
    parent = posixpath.basename(posixpath.dirname(path))
    candidate = stem or base or "file"
    if candidate in existing and parent:
        candidate = f"{parent}-{candidate}"
    original = candidate
    index = 2
    while candidate in existing:
        candidate = f"{original}-{index}"
        index += 1
    return candidate


def _ensure_builtins(reg: ResourceRegistry) -> None:
    from .builtin import install_builtins

    install_builtins(reg)
