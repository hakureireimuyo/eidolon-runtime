"""角色卡加载层：复用 PersonaSeed + eidolon-character，不重定义格式、不重实现容器逻辑。

本层是运行时对底层「协议层 / 扩展层」的唯一消费入口（见 docs/resource-management.md §3.2）：
- 用 personaseed.open() 打开 .seed / .png，得到通用 Package；
- 用 eidolon_character.from_package_with_assets() 把角色数据块解析为类型化 Character。
消费侧靠 MIME 标签（application/x-eidolon-character）路由，鸭子类型接入，不硬依赖 personaseed。
"""
from __future__ import annotations

import os
import sys


def _ensure_siblings_on_path() -> None:
    """开发期把同级兄弟仓库注入 import 路径（与 eidolon-studio 的做法一致）。

    生产期应改为 editable 安装：
        pip install -e ../PersonaSeed ../eidolon-character
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    for name in ("PersonaSeed", "eidolon-character"):
        p = os.path.join(root, name)
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


_ensure_siblings_on_path()

import personaseed as seed  # noqa: E402
from eidolon_character import from_package_with_assets  # noqa: E402
from eidolon_character.reader import MissingCharacterError  # noqa: E402


class CharacterLoadError(Exception):
    """角色卡加载失败（非 PersonaSeed 包 / 包中不含角色模块等）。"""


def load_character_file(path: str):
    """打开 .seed / .png，解析出 (Character, {资源 id: 字节}, manifest)。

    返回：
        character : eidolon_character.model.Character
        assets    : dict[资源 id -> bytes]（图像等原始字节，语义归角色模块）
        manifest  : dict（PersonaSeed 包元信息）
    """
    try:
        pkg = seed.open(path)
    except Exception as exc:  # noqa: BLE001
        raise CharacterLoadError(f"无法打开 PersonaSeed 包：{exc}") from exc
    try:
        character, assets = from_package_with_assets(pkg)
    except MissingCharacterError as exc:
        raise CharacterLoadError(
            "包中不含角色身份模块（缺少 type=application/x-eidolon-character 的数据块）"
        ) from exc
    return character, assets, pkg.manifest
