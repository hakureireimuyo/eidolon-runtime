"""运行时配置：从环境变量读取 LLM 连接信息与数据目录。

设计为「OpenAI 兼容」客户端——base_url / model / api_key 均可替换，
因此同一个运行时既能接 OpenAI，也能接 DeepSeek、通义千问、硅基流动、Ollama 等。
"""
from __future__ import annotations

import os
from pathlib import Path

# --- 数据目录（用户上传 / 临时文件，与源码分离） ---
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = os.environ.get("EIDOLON_RUNTIME_DATA", str(ROOT / "workspace"))
DATA_ROOT = Path(DEFAULT_DATA_ROOT)
DATA_ROOT.mkdir(parents=True, exist_ok=True)

# --- LLM 连接（OpenAI 兼容） ---
LLM_BASE_URL = os.environ.get("EIDOLON_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("EIDOLON_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("EIDOLON_LLM_MODEL", "gpt-4o-mini")
LLM_TEMPERATURE = float(os.environ.get("EIDOLON_LLM_TEMPERATURE", "0.8"))
LLM_MAX_TOKENS = int(os.environ.get("EIDOLON_LLM_MAX_TOKENS", "1024"))


def llm_configured() -> bool:
    """是否已配置可用于真实对话的 LLM（至少要有 API Key）。"""
    return bool(LLM_API_KEY)
