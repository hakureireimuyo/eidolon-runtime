"""资源路由框架：无需预定义工程内容即可加载任意数据。

设计目标（对应 docs/runtime-core-design.md「极小且领域无知」）：

- **自动适配**：包里出现运行时从没见过的数据？按类型标签路由到最匹配的处理器，
  没有专用处理器就退化为可导航的动态资源，绝不加载失败、绝不丢字节。
- **动态创建**：新类型可以在运行时用一行 `define()` 声明出来，或由插件注入，
  不必修改内核代码，也不必提前写死在工程清单里。
- **版本兼容**：数据版本与处理器版本不一致时，先找迁移链自动升级；找不到就按
  前向兼容 / 降级模式加载，并在诊断里说明。

典型用法：

    from runtime.resources import load_package, registry

    space = load_package("world.cart")
    space.report()                                   # 加载了什么、状态如何
    space.first_value("application/x-eidolon-character")

    registry.define("application/x-eidolon-quest", version="1.0", required=("title",))
    space.create("application/x-eidolon-quest", {"title": "寻找灯塔"})
    space.save("world.cart")
"""

from __future__ import annotations

from .builtin import CHARACTER_TYPE, install_builtins
from .dynamic import DynamicResource, infer_schema, unwrap, wrap
from .handler import (
    AutoHandler,
    FunctionHandler,
    JSONHandler,
    RawHandler,
    ResourceHandler,
    SchemaHandler,
    SchemaValidationError,
    TextHandler,
)
from .loader import descriptors_from_package, load_package, open_package
from .model import (
    KIND_ENTRY,
    KIND_FILE,
    KIND_MEDIA,
    KIND_VIRTUAL,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_FORWARD,
    STATUS_GENERIC,
    STATUS_LOADED,
    STATUS_MIGRATED,
    LoadContext,
    ResourceDescriptor,
    ResourceRecord,
    describe_value,
)
from .registry import ResourceRegistry, registry
from .router import ResourceRouter, RouteDecision, router
from .space import ResourceSpace
from .typespec import guess_type, match_score, normalize
from .versioning import Migration, MigrationGraph, Version, VersionRange

# 内置类型在导入时即注册，保证 `load_package` 开箱可用。
install_builtins(registry)

__all__ = [
    "AutoHandler",
    "CHARACTER_TYPE",
    "DynamicResource",
    "FunctionHandler",
    "JSONHandler",
    "KIND_ENTRY",
    "KIND_FILE",
    "KIND_MEDIA",
    "KIND_VIRTUAL",
    "LoadContext",
    "Migration",
    "MigrationGraph",
    "RawHandler",
    "ResourceDescriptor",
    "ResourceHandler",
    "ResourceRecord",
    "ResourceRegistry",
    "ResourceRouter",
    "ResourceSpace",
    "RouteDecision",
    "SchemaHandler",
    "SchemaValidationError",
    "STATUS_DEGRADED",
    "STATUS_ERROR",
    "STATUS_FORWARD",
    "STATUS_GENERIC",
    "STATUS_LOADED",
    "STATUS_MIGRATED",
    "TextHandler",
    "Version",
    "VersionRange",
    "describe_value",
    "descriptors_from_package",
    "guess_type",
    "infer_schema",
    "install_builtins",
    "load_package",
    "match_score",
    "normalize",
    "open_package",
    "registry",
    "router",
    "unwrap",
    "wrap",
]
