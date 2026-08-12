# Eidolon Runtime

Eidolon 生态的**运行时层**第一版实现:通过 Web 可视化界面**加载角色卡(`.seed` / `.png`)**,并**调用 AI 模型完成最基础的角色对话**。

## 在生态中的角色(边界)

```
编辑器层       可视化创建 / 编辑 / 分发(另立项目,GUI 叶子)
引擎运行时 →   消费包、解释角色、驱动对话  ← 本项目
资产类型层     定义角色数据格式 + 消费实现(复用)
协议层         封装 / 索引 / 校验 / 传输(复用)
```

Runtime 是**应用 / 服务**类项目,按 [`docs/environment-isolation.md`](../docs/environment-isolation.md) 约定**独立持有 venv**。
它**不重新定义数据格式**(那是资产类型层的职责),也**不重实现容器逻辑**(那是协议层的职责)——
底层能力通过以下方式即插即用地复用:

- 协议层提供容器打开能力,消费 `.seed` / `.png` 标准包
- 资产类型层提供解析能力,产出类型化角色对象
- 角色身份 = 模板；对话历史 = 运行时状态,二者严格分离

## 功能(V1 最小可用闭环)

- ✅ 上传并加载角色卡(`.seed` / `.png` / `.zip`),展示角色设定与立绘
- ✅ 基于角色设定自动构建 system prompt
- ✅ 调用 AI 模型,进行最基础的一问一答对话(维护上下文历史)
- ✅ 清空对话 / 重新加载

**不属于 V1**:流式输出、多轮记忆持久化、人格网络推理、世界模拟、导出运行时状态回包。

## 目录结构

```
eidolon-runtime/
├── 运行时核心               # 与 Web 解耦,可单独测试
│   ├── 配置模块              # LLM 连接 / 数据目录(环境变量驱动)
│   ├── 加载层                # 角色卡加载(复用协议层 + 资产类型层)
│   ├── AI 服务层             # 工厂模式；默认 DeepSeek,预留语音/视觉扩展
│   └── 对话引擎              # system prompt + 历史 + 对话
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
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

> 开发期加载层会把协议层与资产类型层的代码注入 `sys.path`,
> 因此无需预先安装即可直接复用。生产期建议改为 editable 安装。

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
uvicorn backend.main:app --reload --port 8000
```

打开 http://localhost:8000 :
0. (首次)点击右上角「设置」填入 API Key / 模型等信息并保存(写入 `config.toml`),否则对话会返回 503 提示未配置；
1. 右上角「加载角色卡」选一个 `.seed` / `.png`(可用 `python -m examples.make_sample` 生成 `alice.seed`)；
2. 角色设定与立绘出现在左侧,右侧出现问候语；
3. 在输入框与角色对话。

## 测试

```bash
python -m unittest discover -s tests -t .
```

## 设计要点

- **协议无知 / 扩展层复用**:运行时完全不感知 `.seed` 内部结构,只消费通用 `Package` 与 `Character`。
- **运行时状态隔离**:`Character` 是模板,`RuntimeEngine.history` 是会话级可变状态,不回写角色卡。
- **LLM 可插拔**:对话逻辑与具体模型解耦,换厂商只改环境变量。
