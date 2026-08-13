"""Eidolon Runtime —— Web 后端(FastAPI)。

运行时层通过 Web 暴露「加载角色卡 + 最基础对话」能力:
- 不重新定义数据格式(那是 eidolon-character 的职责)
- 不重实现容器逻辑(那是 PersonaSeed 的职责)
后端只负责:接收上传的 .seed/.png -> 交给 RuntimeEngine 加载；转发对话请求；托管前端。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from runtime import engine as engine_mod  # noqa: F401  (确保包被导入)
from runtime.engine import RuntimeEngine, CharacterLoadError
from runtime.llm import LLMUnconfigured, LLMError
from runtime.llm.config_file import load_llm_config, save_llm_config
from runtime.config import DATA_ROOT, DEVTOOLS_ENABLED
from runtime.resources import registry as default_registry

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Eidolon Runtime", version="0.1.0")

# 托管前端静态资源(单页应用自包含,目录主要用于 index.html)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR), check_dir=False), name="static")

# 全局单会话引擎(V1:一次加载一个角色；重新加载即替换)
engine = RuntimeEngine()


@app.get("/")
def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/character")
def get_character():
    """当前已加载角色卡信息(未加载则返回 {loaded:false})。"""
    return engine.character_info()


@app.post("/api/load")
async def load(file: UploadFile = File(...)):
    """上传 .seed / .png,加载其中的角色卡。"""
    suffix = Path(file.filename or "package.seed").suffix or ".seed"
    tmp = DATA_ROOT / f"upload_{uuid.uuid4().hex}{suffix}"
    try:
        content = await file.read()
        tmp.write_bytes(content)
        info = engine.load(str(tmp))
    except CharacterLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"加载失败:{exc}")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return info


@app.post("/api/chat")
async def chat(payload: dict):
    """发送一条用户消息,返回模型回复与完整对话历史。"""
    message = (payload or {}).get("message", "")
    if not message or not message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    try:
        return engine.chat(message)
    except CharacterLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMUnconfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/reset")
def reset():
    """清空对话历史(保留已加载角色)。"""
    engine.reset()
    return {"ok": True, "history": []}


@app.get("/api/settings")
def get_settings():
    """返回当前 LLM 配置(api_key 原样返回,供本地设置页填充)。"""
    cfg = load_llm_config()
    return {
        "provider": cfg.get("provider", "deepseek"),
        "api_key": cfg.get("api_key", ""),
        "base_url": cfg.get("base_url", ""),
        "model": cfg.get("model", ""),
        "temperature": cfg.get("temperature"),
        "max_tokens": cfg.get("max_tokens"),
        "has_api_key": bool(cfg.get("api_key")),
    }


@app.put("/api/settings")
async def put_settings(payload: dict):
    """写入 LLM 配置到 config.toml(空字符串表示清除该字段)。"""
    allowed = {"provider", "api_key", "base_url", "model", "temperature", "max_tokens"}
    cfg = {k: payload[k] for k in allowed if k in payload}
    # 数值字段归一化
    for num_field in ("temperature", "max_tokens"):
        if num_field in cfg and cfg[num_field] not in ("", None):
            try:
                cfg[num_field] = float(cfg[num_field]) if num_field == "temperature" else int(cfg[num_field])
            except (TypeError, ValueError):
                cfg.pop(num_field, None)
    saved = save_llm_config(cfg)
    return {
        "ok": True,
        "settings": {
            "provider": saved.get("provider", "deepseek"),
            "api_key": saved.get("api_key", ""),
            "base_url": saved.get("base_url", ""),
            "model": saved.get("model", ""),
            "temperature": saved.get("temperature"),
            "max_tokens": saved.get("max_tokens"),
            "has_api_key": bool(saved.get("api_key")),
        },
    }


@app.get("/api/asset/{asset_id}")
def asset(asset_id: str):
    """按资源 id 取回角色引用的图片等字节(语义归角色模块,字节归 PersonaSeed)。"""
    data = engine.assets.get(asset_id)
    if data is None:
        raise HTTPException(status_code=404, detail="资源不存在或该角色未打包此资源字节")
    ctype = engine.asset_types.get(asset_id) or "application/octet-stream"
    return Response(content=data, media_type=ctype)


# ---------------------------------------------------------------------------
# 资源容器 API(工程包 = 多种数据对象的集合,角色只是其中之一)
# ---------------------------------------------------------------------------


@app.get("/api/resources")
def list_resources():
    """当前工程包的全部资源报告(数据对象 + 媒体)。"""
    report = engine.resource_report()
    report["loaded"] = engine.character is not None
    return report


@app.get("/api/resources/{resource_id}")
def get_resource(resource_id: str):
    """按 id 取一份资源记录(含解释后的值)。"""
    if engine.space is None:
        raise HTTPException(status_code=404, detail="尚未加载任何包")
    record = engine.space.get(resource_id)
    if record is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    return record.to_dict(include_value=True)


@app.post("/api/resources")
async def create_resource(payload: dict):
    """在已加载的工程包中动态创建一份资源。"""
    type_value = (payload or {}).get("type")
    if not type_value:
        raise HTTPException(status_code=400, detail="type 不能为空")
    try:
        record = engine.create_resource(
            type_value, payload.get("data"), id=payload.get("id")
        )
    except CharacterLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return record.to_dict(include_value=True)


@app.post("/api/registry/types")
def define_type(payload: dict):
    """运行时动态定义一个资源类型(无需编写 handler 代码)。"""
    type_value = (payload or {}).get("type")
    if not type_value:
        raise HTTPException(status_code=400, detail="type 不能为空")
    handler = default_registry.define(
        type_value,
        version=payload.get("version") or "1.0",
        required=payload.get("required") or (),
        defaults=payload.get("defaults"),
    )
    return {
        "ok": True,
        "type": type_value,
        "version": handler.version,
        "name": handler.name,
    }


@app.get("/api/registry")
def registry_report():
    """当前资源注册表报告(已注册的处理器与迁移链)。"""
    return default_registry.report()


# ---------------------------------------------------------------------------
# 开发者工具(仅在 EIDOLON_RUNTIME_DEVTOOLS=1 时挂载;关闭则零注册)
# ---------------------------------------------------------------------------
if DEVTOOLS_ENABLED:
    from backend.devtools import build_router

    app.include_router(build_router(engine))

    @app.get("/devtools")
    def devtools_index():
        """开发者工具入口页(工具清单)。"""
        return FileResponse(str(FRONTEND_DIR / "devtools" / "index.html"))

    @app.get("/devtools/context")
    def devtools_context():
        """上下文检查器页。"""
        return FileResponse(str(FRONTEND_DIR / "devtools" / "context.html"))
