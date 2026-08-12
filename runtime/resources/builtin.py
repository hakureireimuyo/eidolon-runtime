"""内置处理器:运行时开箱即用的最小类型集合。

内核本身领域无知,"认识角色卡"这件事被隔离在这里,且是**软依赖**——
解释器(eidolon-character-service)没装也不影响启动,角色数据会退化成
通用动态资源。

层级(由具体到宽泛,路由自动按特异度选择):

    application/x-eidolon-character  角色身份 -> Character 对象(经解释器)
    application/json、*/*+json       任意 JSON -> DynamicResource(自适应)
    application/*                    未知结构化类型 -> 自动试 JSON,否则留字节
    text/*                           文本 -> str
    */*                              兜底 -> 原始字节
"""

from __future__ import annotations

from typing import Any

from .dynamic import unwrap
from .handler import AutoHandler, JSONHandler, RawHandler, TextHandler
from .registry import ResourceRegistry

_INSTALLED_FLAG = "_eidolon_builtins_installed"

# 角色类型标签。格式层 eidolon-character 是权威定义方;此处字面量供本包内
# 引用,须与 eidolon-character-service 透传的 CHARACTER_TYPE 值保持一致。
CHARACTER_TYPE = "application/x-eidolon-character"
CHARACTER_VERSIONS = "^1.0"


def install_builtins(registry: ResourceRegistry) -> ResourceRegistry:
    """把内置 handler 装进注册表(幂等)。"""
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
    """注册角色身份处理器(解释器缺失时静默跳过,软降级)。"""
    try:
        from eidolon_character_service import CHARACTER_TYPE as type_value
        from eidolon_character_service import interpret_character
    except Exception:  # noqa: BLE001 - 软依赖,缺失即降级
        return False

    @registry.handler(
        type_value,
        versions=CHARACTER_VERSIONS,
        name="eidolon-character",
        description="角色身份模块(经 eidolon-character-service 解释)",
    )
    def load_character(data: Any, descriptor: Any, context: Any = None):
        """把角色数据块解释为类型化 Character 对象。"""
        character, assets = interpret_character(
            unwrap(data), getattr(context, "media", None)
        )
        # 把角色声明的媒体字节挂到上下文,供上层按需取用
        if assets and context is not None:
            context.extras.setdefault("character_assets", {}).update(assets)
        return character

    return True
