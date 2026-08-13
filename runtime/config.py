"""运行时配置:数据目录与开发者模式开关。

LLM 连接配置已下沉到 AI 服务层(runtime/llm/),按服务分别读取环境变量,
不再集中在此。此处仅管理与源码分离的用户数据根目录。
"""
from __future__ import annotations

import os
from pathlib import Path

# --- 数据目录(用户上传 / 临时文件,与源码分离) ---
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = os.environ.get("EIDOLON_RUNTIME_DATA", str(ROOT / "workspace"))
DATA_ROOT = Path(DEFAULT_DATA_ROOT)
DATA_ROOT.mkdir(parents=True, exist_ok=True)


# --- 开发者模式开关(启用 devtools 等开发工具,默认关闭) ---
def _parse_devtools_flag(value: str) -> bool:
    """解析 EIDOLON_RUNTIME_DEVTOOLS 开关值(1/true/yes/on 为开)。"""
    return value.strip().lower() in ("1", "true", "yes", "on")


DEVTOOLS_ENABLED = _parse_devtools_flag(
    os.environ.get("EIDOLON_RUNTIME_DEVTOOLS", "")
)
