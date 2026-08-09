# Eidolon Runtime

Eidolon 生态的**运行时层**第一版实现：通过 Web 可视化界面**加载角色卡（`.seed` / `.png`）**，并**调用 AI 模型完成最基础的角色对话**。

## 在生态中的角色（边界）

```
使用者界面层   eidolon-studio    可视化创建 / 编辑 / 分发（另立项目，GUI 叶子）
运行时层   →  eidolon-runtime   消费包、解释角色、驱动对话  ← 本项目
扩展层       eidolon-character  定义 character.json 格式 + 消费实现（复用）
协议层       PersonaSeed        封装 / 索引 / 校验 / 传输（复用）
```

Runtime 是**应用 / 服务**类项目，按 [`docs/environment-isolation.md`](../docs/environment-isolation.md) 约定**独立持有 venv**。
它**不重新定义数据格式**（那是 eidolon-character 的职责），也**不重实现容器逻辑**（那是 PersonaSeed 的职责）——
底层能力通过以下方式即插即用地复用：

- `personaseed.open()` 打开 `.seed` / `.png`
- `eidolon_character.from_package_with_assets()` 解析出类型化 `Character`
- 角色身份 = 模板；对话历史 = 运行时状态，二者严格分离

## 功能（V1 最小可用闭环）

- ✅ 上传并加载角色卡（`.seed` / `.png` / `.zip`），展示角色设定与立绘
- ✅ 基于角色设定自动构建 system prompt
- ✅ 调用 AI 模型，进行最基础的一问一答对话（维护上下文历史）
- ✅ 清空对话 / 重新加载

**不属于 V1**：流式输出、多轮记忆持久化、人格网络推理、世界模拟、导出运行时状态回包。

## 目录结构

```
eidolon-runtime/
├── runtime/                 # 运行时核心逻辑（与 Web 解耦，可单独测试）
│   ├── config.py            # LLM 连接 / 数据目录（环境变量）
│   ├── loader.py            # 角色卡加载层（复用 PersonaSeed + eidolon-character）
│   ├── llm.py               # AI 模型调用层（OpenAI 兼容）
│   └── engine.py            # 对话引擎（system prompt + 历史 + 对话）
├── backend/main.py          # FastAPI 后端：加载 / 对话 / 静态托管
├── frontend/index.html      # 单页对话界面（暗色，无构建步骤）
├── examples/make_sample.py  # 生成一个示例角色包 alice.seed
├── tests/test_runtime.py     # 最小测试（零网络）
├── workspace/               # 用户数据（gitignored）
└── requirements.txt         # venv 依赖
```

## 运行

### 1. 安装依赖（独立 venv）

```bash
cd eidolon-runtime
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

> 开发期 `runtime/loader.py` 会把同级 `../PersonaSeed` 与 `../eidolon-character` 注入 `sys.path`，
> 因此无需预先安装这两个库即可直接复用。生产期建议改为 editable 安装：
> `pip install -e ../PersonaSeed ../eidolon-character`。

### 2. 配置 AI 模型（OpenAI 兼容）

运行时使用 **OpenAI 兼容**客户端，可对接 OpenAI、DeepSeek、通义千问、硅基流动、Ollama 等。
通过环境变量配置（写入 `.env` 或 shell 环境）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EIDOLON_LLM_API_KEY` | _(空)_ | **必填**才能真实对话 |
| `EIDOLON_LLM_BASE_URL` | `https://api.openai.com/v1` | 端点；DeepSeek 用 `https://api.deepseek.com/v1` |
| `EIDOLON_LLM_MODEL` | `gpt-4o-mini` | 模型名；DeepSeek 用 `deepseek-chat` |
| `EIDOLON_LLM_TEMPERATURE` | `0.8` | 创造性 |
| `EIDOLON_LLM_MAX_TOKENS` | `1024` | 回复长度上限 |

示例（DeepSeek）：

```bash
export EIDOLON_LLM_BASE_URL=https://api.deepseek.com/v1
export EIDOLON_LLM_API_KEY=sk-xxxx
export EIDOLON_LLM_MODEL=deepseek-chat
```

> 未配置 Key 时，角色卡仍可正常加载与展示；发起对话会返回明确提示，不会崩溃。

### 3. 启动

```bash
uvicorn backend.main:app --reload --port 8000
```

打开 http://localhost:8000 ：
1. 右上角「加载角色卡」选一个 `.seed` / `.png`（可用 `python -m examples.make_sample` 生成 `alice.seed`）；
2. 角色设定与立绘出现在左侧，右侧出现问候语；
3. 在输入框与角色对话。

## 测试

```bash
python -m unittest discover -s tests -t .
```

## 设计要点

- **协议无知 / 扩展层复用**：运行时完全不感知 `.seed` 内部结构，只消费通用 `Package` 与 `Character`。
- **运行时状态隔离**：`Character` 是模板，`RuntimeEngine.history` 是会话级可变状态，不回写角色卡。
- **LLM 可插拔**：对话逻辑与具体模型解耦，换厂商只改环境变量。
