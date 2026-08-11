"""内置处理器：运行时开箱即用的最小类型集合。

内核本身领域无知，"认识角色卡"这件事被隔离在这里，且是**软依赖**——
eidolon-character 没装也不影响启动，角色数据会退化成通用动态资源。

层级（由具体到宽泛，路由自动按特异度选择）：

    application/x-eidolon-character  角色身份 -> Character 对象
    application/json、*/*+json       任意 JSON -> DynamicResource（自适应）
    application/*                    未知结构化类型 -> 自动试 JSON，否则留字节
    text/*                           文本 -> str
    */*                              兜底 -> 原始字节
"""

from __future__ import annotations

from typing import Any

from .dynamic import unwrap
from .handler import AutoHandler, JSONHandler, RawHandler, TextHandler
from .registry import ResourceRegistry

_INSTALLED_FLAG = "_eidolon_builtins_installed"

CHARACTER_TYPE = "application/x-eidolon-character"
CHARACTER_VERSIONS = "^1.0"


def install_builtins(registry: ResourceRegistry) -> ResourceRegistry:
    """把内置 handler 装进注册表（幂等）。"""
    if getattr(registry, _INSTALLED_FLAG, False):
        return registry
    setattr(registry, _INSTALLED_FLAG, True)

    registry.register(RawHandler())
    registry.register(TextHandler())
    registry.register(AutoHandler())
    registry.register(JSONHandler())
    _install_character(registry)
    return registry


def _install_character(registry: ResourceRegistry) -> bool:
    """注册角色身份处理器（eidolon-character 缺失时静默跳过）。"""
    try:
        from eidolon_character.model import CHARACTER_TYPE as type_value, from_dict
    except Exception:  # noqa: BLE001 - 软依赖，缺失即降级
        return False

    @registry.handler(
        type_value,
        versions=CHARACTER_VERSIONS,
        name="eidolon-character",
        description="角色身份模块（eidolon-character）",
    )
    def load_character(data: Any, descriptor: Any, context: Any = None):
        """把角色数据块解析为类型化 Character 对象。"""
        character = from_dict(unwrap(data))
        # 把包内媒体字节按 assets 声明挂到上下文，供上层按需取用
        if context is not None and getattr(context, "media", None):
            declared = {a.id for a in getattr(character, "assets", []) or []}
            context.extras.setdefault("character_assets", {}).update(
                {k: v for k, v in context.media.items() if k in declared}
            )
        return character

    return True
