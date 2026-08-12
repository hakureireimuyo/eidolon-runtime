# LLM Gateway 与 Context Management 抽象层

> 本文档记录两个平级抽象层的设计决策:LLM Gateway(封装 provider 差异)与 Context Management(分层缓存友好的上下文管理)。
> 二者是实现多 agent 系统的底层核心支撑。
> 相关文档:[运行时核心设计](./runtime-core-design.md) · [状态模型、上下文管理与缓存](./state-and-context.md) · [上下文管理设计](./context-management.md) · [多智能体与多模态系统](./multi-agent-multimodal.md)

## 1. 层级定位

```
eidolon-runtime(engine / 未来 agents)
        │                        │
        ▼                        ▼
   LLM Gateway           Context Manager
   (抽象层 A)            (抽象层 B)
        │                        │
        ▼                        ▼
   LLM Service Layer      Context IR / Compiler
   (runtime/llm/)          / Buffer / Cache
        │
        ▼
   DeepSeekService / 未来其他 provider
```

两个抽象层**平级**,各自封装一个维度的复杂性:
- **LLM Gateway**:封装「谁来生成」(provider 差异、参数管理、API 调用)。
- **Context Manager**:封装「给什么内容」(上下文分层、缓存布局、增量更新)。

eidolon-runtime(engine 和未来 agents)只与这两层交互,**不直接操作**底层 LLM 服务或 messages 拼接。

## 2. LLM Gateway(`runtime/llm_gateway/`)

### 2.1 职责

| 职责 | 说明 |
|------|------|
| **封装 provider 差异** | engine 只构造 `LLMRequest`、消费 `LLMResponse`,不感知 DeepSeek / OpenAI / 本地模型 |
| **结构化 I/O** | 输入 `LLMRequest(messages, temperature?, max_tokens?, stream?)`,输出 `LLMResponse(content, provider, ...)` |
| **多 agent 就绪** | Gateway 无会话状态,多 agent 共享一个实例 |
| **参数管理** | per-request 参数在 request 上声明；Gateway 尝试传递到底层,不支持则优雅降级 |

### 2.2 核心类型

```python
@dataclass
class LLMRequest:
    messages: list[dict]           # 由 ContextManager 编译产出
    temperature: float | None      # per-request 覆盖(可选)
    max_tokens: int | None         # per-request 覆盖(可选)
    stream: bool = False

@dataclass
class LLMResponse:
    content: str                   # 模型文本回复
    provider: str                   # 提供服务的 provider 名
    finish_reason: str | None       # 结束原因
    usage: dict                     # token 用量
```

### 2.3 不负责

- 上下文编译(ContextManager 职责)
- 会话状态维护(engine / ContextManager 职责)
- provider 注册与发现(runtime/llm/ 的 ServiceFactory 职责)

### 2.4 与现有 LLM 服务层的关系

`runtime/llm/`(AIService + ServiceFactory + DeepSeekService)保持不变。LLM Gateway 是**其上的薄封装**:

```
LLMGateway.complete(request)
  → _resolve_service(request)     # 工厂/注入获取 AIService
  → service.chat(messages)        # 委托底层
  → LLMResponse(content, provider)
```

新增 provider 时只需在 `runtime/llm/` 注册新 `AIService` 子类,Gateway 和 engine 零改动。

## 3. Context Management(`runtime/context/`)

### 3.1 职责

对齐 [上下文管理设计](./context-management.md) 的全部设计决策:

| 职责 | 对齐文档章节 |
|------|-------------|
| 分层管理(static / low / mid / high) | §2 上下文按更新频率分层 |
| 增量更新(只改变化部分) | §4 状态演化,而非状态重建 |
| 缓存友好布局(稳定在前) | §6 缓存友好的上下文布局 |
| Context IR 中间表示 | §5 上下文编译管线 |
| 多级缓存(L0/L1 system 前缀缓存) | §7 多级缓存 |
| 对话缓冲管理 | §4 高频层 |

### 3.2 架构

```
ContextManager
├── ContextIR              ← 中间表示(segment 集合)
│   ├── ContextSegment(text, layer, tag, role, cacheable)
│   └── ContextLayer(STATIC=0, LOW=1, MID=2, HIGH=3)
├── ConversationBuffer     ← 对话缓冲区(高频层)
│   └── ConversationTurn(role, content, ts)
└── ContextCompiler        ← 编译器(IR + 对话 → messages)
    ├── compile()          → list[dict](OpenAI 风格)
    ├── compile_prefix()   → str(system 前缀,用于缓存)
    └── estimate_cache_boundary() → dict(诊断信息)
```

### 3.3 编译规则(缓存友好布局)

编译产出 messages 时按以下布局排列:

```
[system: static + low + mid 合并]   ← 前缀缓存区域(多轮间不变 → KV Cache 命中)
[非 system 片段]                     ← 按稳定性排序
[conversation: user/assistant 消息]  ← 高频层,每轮追加
```

核心原则:**越靠前越稳定、越靠后越动态**。变化只发生在尾部,保证 LLM 前缀缓存可复用。

### 3.4 不负责

- 状态的内部表示(各能力子项目职责,如 eidolon-mind 维护情绪状态)
- LLM API 调用(LLM Gateway 职责)
- 记忆的存储和检索(eidolon-memory 子项目职责)
- 状态转移函数(各 Processor 职责)

ContextManager 只负责**收集各层状态输出 → 编译为模型可用上下文**。

## 4. Engine 重构

### 4.1 重构前(直接操作)

```python
class RuntimeEngine:
    def chat(self, user_message):
        system_prompt = build_system_prompt(self.character)
        messages = [{"role": "system", "content": system_prompt}]
        messages += [{"role": m.role, "content": m.content} for m in self.history]
        messages.append({"role": "user", "content": user_message})
        reply = llm_chat(messages)  # 直接调 LLM 服务层
```

### 4.2 重构后(两层抽象)

```python
class RuntimeEngine:
    def __init__(self, *, gateway=None, context=None):
        self._gateway = gateway or LLMGateway()
        self._context = context or ContextManager()

    def load(self, path):
        # ... 加载角色卡 ...
        system_prompt = build_system_prompt(character)
        self._context.set_static("character_prompt", system_prompt)
        self._context.reset_conversation()

    def chat(self, user_message):
        self._context.add_message("user", user_message)
        messages = self._context.compile()         # ← 上下文管理
        response = self._gateway.complete(         # ← LLM 网关
            LLMRequest(messages=messages)
        )
        self._context.add_message("assistant", response.content)
        return {"reply": response.content, ...}
```

engine 不再直接操作 messages 拼接或 LLM 服务调用——全部委托给两个抽象层。

### 4.3 错误回滚

当 LLM 调用失败时,engine 回滚 ContextManager 的对话缓冲区,避免留下「用户消息已加入上下文但没有回复」的不一致状态。

## 5. 多 Agent 扩展路径

这两个抽象层是为多 agent 系统设计的底层核心:

### 5.1 共享 Gateway + 独立 Context

```
                     ┌─── Agent A (Dialogue)  ─── ContextManager A ───┐
LLMGateway (共享)  ───┤                                                     ├── Event Bus
                     ├─── Agent B (Emotion)   ─── ContextManager B ───┤  │
                     │                                                     │  ▼
                     └─── Agent C (Memory)    ─── ContextManager C ───┘  State Store
```

- 多个 agent 共享一个 `LLMGateway`(无状态,天然安全共享)。
- 每个 agent 持有自己的 `ContextManager`(各自维护独立的上下文视图)。
- 共享状态通过 Event Bus + State Store 同步(对齐 `runtime-core-design.md` §5)。

### 5.2 上下文 = 状态的投影

当某个 agent 的状态变化(如 Emotion Agent 更新 `sadness:+0.3`),它只需更新自己的 ContextManager 对应层:

```python
emotion_agent._context.set_mid("emotion_state", "角色感到悲伤")
```

编译时只重新编译受影响的片段,system 前缀大部分可复用(KV Cache 命中)。

### 5.3 未来扩展点

| 扩展 | 实现位置 | 影响 |
|------|---------|------|
| 流式输出 | `LLMGateway.complete_stream()` | engine / agent 零改动 |
| 工具调用 | `LLMRequest.tools` + `LLMResponse.tool_calls` | ContextManager 编译时加入工具声明 |
| 多模态输入 | `LLMRequest.messages` 的 content 支持多模态块 | 底层 AIService 已预留 |
| 记忆检索缓存 | `ContextManager` + `ContextLayer.LOW` 的情境指纹 | 新增 L3 缓存层 |
| 状态转移函数 | 各能力子项目 Processor | ContextManager 只消费状态投影 |

## 6. 设计边界总结

| 关注点 | 归属层 |
|--------|--------|
| provider 选择、API 调用、参数传递 | LLM Gateway |
| messages 拼接、分层布局、缓存优化 | Context Manager |
| 对话历史管理(窗口截断) | Context Manager (ConversationBuffer) |
| 角色卡加载、prompt 构建 | eidolon-character-service |
| 资源路由(包加载) | runtime/resources/ |
| 状态内部表示、转移函数 | 各能力子项目(eidolon-mind 等) |
| 会话编排(谁先调谁) | RuntimeEngine |

**原则:稳定部分进核心(Gateway + Manager),可变部分成扩展(各子项目)。**
