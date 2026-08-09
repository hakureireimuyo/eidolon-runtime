"""AI 服务配置文件读写（简单实现）。

配置文件为项目根目录下的 `config.toml`（可用环境变量 EIDOLON_RUNTIME_CONFIG 覆盖路径）。
仅包含一个 `[llm]` 段落，存放 provider / api_key / base_url / model / temperature / max_tokens。

- 读取用标准库 tomllib（Python 3.11+）。
- 写入用极简 TOML 序列化（仅扁平键值，无嵌套），避免引入第三方依赖。
配置文件含密钥，已被 .gitignore 忽略；仓库内提供 config.example.toml 作为模板。
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore

DEFAULT_PATH = Path(
    os.environ.get(
        "EIDOLON_RUNTIME_CONFIG",
        str(Path(__file__).resolve().parent.parent.parent / "config.toml"),
    )
)

# [llm] 段落支持的字段
LLM_FIELDS = ("provider", "api_key", "base_url", "model", "temperature", "max_tokens")


def config_path() -> Path:
    # 每次调用都读取环境变量，便于测试/运行时切换配置路径。
    return Path(
        os.environ.get(
            "EIDOLON_RUNTIME_CONFIG",
            str(Path(__file__).resolve().parent.parent.parent / "config.toml"),
        )
    )


def load_llm_config() -> dict:
    """读取 [llm] 段；文件不存在或无法解析时返回空字典。"""
    p = config_path()
    if not p.exists() or tomllib is None:
        return {}
    try:
        with open(p, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}
    llm = data.get("llm", {}) or {}
    return {k: llm[k] for k in LLM_FIELDS if k in llm}


def save_llm_config(cfg: dict) -> dict:
    """合并写入 [llm] 段（仅覆盖提供的字段；空值/None 表示清除该字段）。

    返回写入后的 [llm] 段。
    """
    p = config_path()
    existing: dict = {}
    if p.exists() and tomllib is not None:
        try:
            with open(p, "rb") as f:
                existing = tomllib.load(f)
        except Exception:
            existing = {}
    llm = dict(existing.get("llm", {}) or {})
    for k, v in cfg.items():
        if v is None or v == "":
            llm.pop(k, None)
        else:
            llm[k] = v
    existing["llm"] = llm
    p.write_text(_dump_toml(existing), encoding="utf-8")
    return llm


def _dump_toml(data: dict) -> str:
    lines: list[str] = []
    for section, kv in data.items():
        lines.append(f"[{section}]")
        for k, v in kv.items():
            lines.append(_dump_kv(k, v))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dump_kv(k: str, v) -> str:
    if isinstance(v, bool):
        return f"{k} = {'true' if v else 'false'}"
    if isinstance(v, (int, float)):
        return f"{k} = {v}"
    return f'{k} = "{v}"'
