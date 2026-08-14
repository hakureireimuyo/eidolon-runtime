# Eidolon Runtime

Eidolon 生态的**运行时层**第一版实现:通过 Web 可视化界面**加载角色卡(`.cart` / `.png` / `.zip`)**,并**调用 AI 模型完成最基础的角色对话**。

## 在生态中的角色(边界)

```
编辑器层       可视化创建 / 编辑 / 分发(另立项目,GUI 叶子)
引擎运行时 →   消费包、驱动对话(组合入口)  ← 本项目
运行时解释器   角色数据块 → 内存对象 + prompt 编译(复用 eidolon-character-service)
资产类型层     定义角色数据格式(零依赖,复用)
协议层         封装 / 索引 / 校验 / 传输(复用)
```

Runtime 是**应用 / 服务**类项目,按 [`docs/environment-isolation.md`](../docs/environment-isolation.md) 约定**独立持有 venv**。
它**不重新定义数据格式**(那是资产类型层的职责),也**不重实现容器逻辑**(那是协议层的职责)——
底层能力通过以下方式即插即用地复用:

- 协议层提供容器打开能力,消费 `.cart` / `.png` 标准包
- 解释器(eidolon-character-service)把角色数据块解释为类型化对象并编译 prompt,组合入口按类型标签消费
- 角色身份 = 模板；对话历史 = 运行时状态,二者严格分离

## 功能(V1 最小可用闭环)

- ✅ 上传并加载角色卡(`.cart` / `.png` / `.zip`),展示角色设定与立绘
- ✅ 基于角色设定自动构建 system prompt
- ✅ 调用 AI 模型对话(维护上下文历史)
- ✅ **流式输出**:SSE 事件流逐字渲染,支持加粗 / 斜体样式片段(前端永不见原始占位符号)
- ✅ **工具调用内循环**:tool calls(LLM 请求 → 引擎执行 → 结果回流再生成,多轮自动循环)
- ✅ **内嵌占位符协议**(`⟦ ⟧`):只读值注入(`⟦time⟧` / `⟦char:name⟧`)与样式指令(`⟦b:…⟧`),解析后入史即固定
- ✅ 清空对话 / 重新加载

**不属于 V1**:多轮记忆持久化、人格网络推理、世界模拟、导出运行时状态回包。

> 设计规范见 [docs/streaming-event-loop-placeholder.md](docs/streaming-event-loop-placeholder.md)。
> 已知待办:system prompt 尚未注入 `⟦⟧` 语法示例段(规范 §4.1 `inline_syntax`);工具注册表默认为空(协议与循环先行,工具逐步接入)。

### Web API 一览

| 端点 | 说明 |
|------|------|
| `GET /api/character` | 当前角色卡信息 + 历史(assistant 消息附 `segments` 样式片段) |
| `POST /api/load` | 上传角色包加载 |
| `POST /api/chat` | 同步对话(兼容旧接口,内部复用流式路径) |
| `POST /api/chat/stream` | **流式对话(SSE)** |
| `POST /api/reset` / `GET /api/characters` / `POST /api/select` | 清空对话 / 角色列表 / 切换会话 |
| `GET / PUT /api/settings` | 读取 / 写入 LLM 配置 |

`POST /api/chat/stream` 返回 `text/event-stream`(每行 `data: <JSON>`):

| 事件 | payload | 说明 |
|------|---------|------|
| `chat.start` | `{session_key}` | 流开始 |
| `loop.turn` | `{turn, reason}` | 内循环第 N 轮(`start` / `tool_result` / `continue`) |
| `text.delta` | `{delta, style}` | **解析后**文本增量;`style`: `plain` / `bold` / `italic` |
| `tool.call` | `{id, name, label, args}` | LLM 请求执行工具 |
| `tool.result` | `{id, name, ok, error?}` | 执行结果(正文不回显,由模型重新表达) |
| `chat.done` | `{reply: {text, segments}, history}` | 正常结束(segments 供前端最终同步) |
| `chat.error` | `{code, message}` | 失败(`not_loaded` / `unconfigured` / `llm_error` / `cancelled` / `max_turns` / `empty_reply`) |

客户端断开连接即取消生成:内循环在检查点退出,已执行工具的世界效果不回滚(程序真实性),只回滚上下文。

## 目录结构

```
eidolon-runtime/
├── 运行时核心               # 与 Web 解耦,可单独测试
│   ├── 配置模块              # LLM 连接 / 数据目录(环境变量驱动)
│   ├── 加载层                # 数据解析容器:整包摊平(runtime.resources)+ 按类型标签路由解释
│   ├── AI 服务层             # 工厂模式；默认 DeepSeek(含 chat_stream 流式契约),预留语音/视觉扩展
│   ├── LLM 网关              # provider 封装:补全 / 流式事件流(tool_call delta 组装)
│   ├── 内联协议解析层        # runtime.inline:⟦⟧ 占位符(只认语法不认程序,解析失败静默)
│   ├── 工具层                # runtime.tools:ToolSpec / ToolRegistry
│   ├── 事件内循环            # runtime.agent_loop:LLM ↔ 工具调度(transient 不入历史)
│   └── 对话引擎              # system prompt + 历史 + 对话(同步复用流式路径)
├── Web 后端                  # FastAPI:加载 / 对话 / 静态托管
├── 前端                      # 单页对话界面(暗色,无构建步骤)
├── 示例                      # 生成示例角色包
├── 测试                      # 最小测试(零网络)
├── 用户数据目录              # gitignored
├── 本地 AI 配置              # gitignored；可用环境变量覆盖路径
├── 配置模板                  # 入库
└── 依赖声明
```

## 运行

### 1. 安装依赖(独立 venv)

```bash
cd eidolon-runtime
uv sync          # 首次:创建 .venv 并安装依赖(含测试依赖)
```

> 协议层(cartridge)、资产类型层(eidolon-character)与解释器(eidolon-character-service)
> 均在 `pyproject.toml` 中以 **git 源(pin rev)** 声明为正规第三方依赖,无需
> `sys.path` 注入,monorepo 检出与独立 clone 行为一致。加载工程包走数据解析容器
> `runtime.resources`(打开一次、全量解析),角色数据块经
> `eidolon-character-service`(解释器)按类型标签路由解释;
> 组合层不直接 import 格式层。

### 2. 配置 AI 服务(工厂模式)

AI 调用层采用**工厂模式**(AI 服务层):按名称装配服务实例,便于后续扩展厂商与能力。
**当前默认仅注册 DeepSeek**；语音 / 视觉等多模态能力在 `AIService` 基类上作为扩展点预留,
默认实现直接抛出 `UnsupportedCapability`,待实现对应服务后通过工厂注册即可。

通过环境变量配置(写入 `.env` 或 shell 环境):

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EIDOLON_LLM_PROVIDER` | `deepseek` | 选用的服务名(工厂注册键) |
| `EIDOLON_DEEPSEEK_API_KEY` | _(空)_ | **必填**才能真实对话(兜底也可用 `EIDOLON_LLM_API_KEY`) |
| `EIDOLON_DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek 端点 |
| `EIDOLON_DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `EIDOLON_LLM_TEMPERATURE` | `0.8` | 创造性(通用兜底) |
| `EIDOLON_LLM_MAX_TOKENS` | `1024` | 回复长度上限(通用兜底) |

示例:

```bash
export EIDOLON_LLM_PROVIDER=deepseek
export EIDOLON_DEEPSEEK_API_KEY=sk-xxxx
# 可选:export EIDOLON_DEEPSEEK_MODEL=deepseek-reasoner
```

#### 配置文件 + 设置页(推荐,简单实现)

除环境变量外,运行时也支持把配置写入项目根目录的 `config.toml`(**已被 `.gitignore` 忽略**,不会入库)。
仓库内提供 `config.example.toml` 作为模板,复制为 `config.toml` 后填写即可:

```toml
[llm]
provider  = "deepseek"
api_key   = "sk-xxxx"
base_url  = "https://api.deepseek.com/v1"
model     = "deepseek-chat"
temperature = 0.8
max_tokens  = 1024
```

更方便的是**应用内设置页**:启动后点击右上角「设置」,直接填写服务商 / API Key / 端点 / 模型 / 温度 /
最大 Token,点保存即写入 `config.toml`(留空则清除对应字段)。底层实现:

- 配置模块 —— 读取用标准库 `tomllib`,写入用极简 TOML 序列化(零第三方依赖)；
  配置路径可用环境变量 `EIDOLON_RUNTIME_CONFIG` 覆盖(便于测试 / 多环境)。
- Web 后端 —— `GET /api/settings` 读取、`PUT /api/settings` 写入与字段归一化。
- 配置优先级(每个字段独立):**显式参数 > 环境变量 `EIDOLON_LLM_<FIELD>` > `config.toml` `[llm]` 段 > 服务内置默认**；
  `provider` 优先级:**显式 > 环境变量 > 配置段 > `deepseek`**。

**多模态扩展(未来)**:在 AI 服务层下新增子类(如语音 / 视觉服务),
覆写对应多模态方法,并在服务工厂注册表中追加一行注册即可,对话引擎 / 前端无需改动；
上层可探测服务能力以决定启用哪些 UI。消息内容已支持
OpenAI 风格多模态内容块,数据模型层面视觉能力已就绪。

> 未配置 Key 时,角色卡仍可正常加载与展示；发起对话会返回明确提示(HTTP 503),不会崩溃。

### 3. 启动

```bash
bash scripts/start.sh        # 一键启动(默认端口 8010,与 Studio 的 8000 错开;停止:bash scripts/start.sh stop)
# 或手动:
uv run uvicorn backend.main:app --reload --port 8000
```

打开 http://127.0.0.1:8010 (手动启动则为 8000):
0. (首次)点击右上角「设置」填入 API Key / 模型等信息并保存(写入 `config.toml`),否则对话会返回 503 提示未配置；
1. 右上角「加载角色卡」选一个 `.cart` / `.png`(可用 `uv run python -m examples.make_sample` 生成示例包 `alice.cart`)；
2. 角色设定与立绘出现在左侧,右侧出现问候语；
3. 在输入框与角色对话。

## 测试

```bash
uv run python -m unittest discover -s tests -t .
```

## 设计要点

- **协议无知 / 扩展层复用**:运行时完全不感知 `.cart` 内部结构,只消费通用 `Package` 与 `Character`。
- **运行时状态隔离**:`Character` 是模板,`RuntimeEngine.history` 是会话级可变状态,不回写角色卡。
- **LLM 可插拔**:对话逻辑与具体模型解耦,换厂商只改环境变量。
- **LLM 输出 = 事件流**:流式文本 / 工具调用 / 内嵌占位符统一在一条事件流上;解析层(⟦⟧ 协议)只认语法、不认识任何程序,解析失败静默替换;解析后文本入史即固定、永不重解析(前缀缓存友好)。
- **工具 = 程序接口**:函数调用语义一律走 tool calls;工具消息为循环局部(transient),尾部拼接、不入用户可见历史;错误文本回流模型自行挽救,世界不回滚。
- **前端只见渲染结果**:前端只消费 `segments`([{text, style}]),原始符号与工具痕迹永不到达前端。
