# 流式调度:事件协议、工具内循环与内嵌占位符

> 本文档回答三个问题:**前后端如何流式异步输出、如何在同一流中支持工具调用与 agent 事件内循环、LLM 输出中的内嵌占位符如何被解释与渲染。**
> 相关文档:[引擎核心:上下文流动的管理者](./engine-core.md) · [LLM Gateway 与 Context Management 抽象层](./llm-gateway-and-context.md) · [运行时核心设计](./runtime-core-design.md)

## 1. 核心命题

> **LLM 的输出不是一段纯文本,而是一条事件流:流经解析器、解释器、工具执行器,只有渲染结果到达用户。**

三个机制统一在一条流上:

| 机制 | 方向 | 语义 |
|------|------|------|
| **流式文本** | LLM → 前端 | 表达输出,增量渲染 |
| **工具调用(tool call)** | LLM → 程序 → LLM | 结构化指令:带参数、可验证、可多轮循环 |
| **内嵌占位符(inline placeholder)** | LLM → 解释器 → 前端 | 表现层通道:样式指令 + 只读值注入(不承担函数调用) |

工具调用是 engine-core.md §5「LLM 反向指挥程序」的落地通道;占位符是表现层自身的通道(样式与只读值引用),不承担指挥职责。共同约束:**程序接口对用户不可见**——工具的原始调用与结果、占位符的原始符号,永不渲染到前端。

## 2. 总体数据流

```
用户消息
   │
   ▼
Generator.generate_stream() ──┐
   │                          │ 每轮:compile(context + transient) → LLMRequest(tools)
   ▼                          │
AgentLoop(事件内循环)◄────────┘
   │  provider 流式 chunk(raw text delta / tool_call delta)
   ▼
StreamParser(⟦⟧ 增量状态机)
   │  ├─ 普通文本 → text.delta
   │  ├─ 指令 ⟦b:…⟧ → 渐进 text.delta(style=bold)
   │  └─ 注入 ⟦time⟧ → 上层注入 resolver 应答(未接入 → 静默丢弃)
   ▼
事件流 → SSE → 前端 segments 渲染
轮末:解析后文本(值已替换)→ ConversationBuffer → 入史即固定,永不重解析

tool_call delta → 组装 → ToolRegistry 执行 → tool result 消息回流
   → transient 消息尾部拼接 → 下一轮 LLM 调用(loop.turn)
```

## 3. 传输层:SSE(事件信封与传输无关)

**决策:V1 用 SSE(`POST /api/chat/stream` → `text/event-stream`)。**

- LLM 输出天然单向,SSE 足够;FastAPI `StreamingResponse` 改动最小;前端 `EventSource` 自带断线重连。
- 事件信封与传输无关:未来世界事件推送(不依附某次 chat 请求)需要双向时,换 WebSocket **只换传输,事件类型不变**。V1 不做 WS,是明确的取舍而非妥协。
- 用户取消 = 前端关闭连接 → 后端检测断开 → 循环在下一检查点退出(见 §7 取消语义)。

### 3.1 事件类型

每个 SSE `data:` 行一条 JSON 事件:

| 事件 | payload | 说明 |
|------|---------|------|
| `chat.start` | `{session_key}` | 流开始 |
| `loop.turn` | `{turn, reason}` | 内循环第 N 轮 LLM 调用;`reason`: `start` / `tool_result` / `continue` |
| `text.delta` | `{delta, style}` | **只含解析后内容**;`style`: `plain` / `bold` / `italic` |
| `tool.call` | `{id, name, label, args}` | LLM 请求执行工具(前端显示状态条,如「正在:开门」) |
| `tool.result` | `{id, name, ok, error?}` | 执行结果(结果正文不回显,由模型重新表达) |
| `chat.done` | `{reply: {text, segments}, history}` | 正常结束 |
| `chat.error` | `{code, message}` | 失败(LLM 未配置 / 超时等) |

### 3.2 前端渲染模型:segments

- 每条 bot 消息是一个**片段列表** `segments: [{text, style}]`;`text.delta` 追加到当前片段,相邻同风格合并。
- 历史与回放(`chat.done.history`、`/api/character`)同样下发 segments——流式渲染与历史重渲染**同一条渲染路径**,前端永远只渲染 segments,**永远接触不到原始符号**。
- 消息记录双表示:`content`(**解析后纯文本**:值已替换、样式剥离,供 LLM 上下文)与 `segments`(解析后带样式,供显示)。前端只读后者,上下文只读前者。
- **解析一次,入史即固定**:LLM 输出直接进入解析层,替换完成后才参与历史上下文的构建;原文(含 `⟦⟧`)解析后即丢弃(诊断模式可留存,不入上下文)。入史的内容此后**永不修改、永不重解析**——若在编译期对历史重解析,`⟦time⟧` 每次解析值都变,内容漂移、前缀缓存击穿。固定不变的正文才是稳定 KV 缓存命中的前提。

## 4. 内嵌占位符规范

### 4.1 符号:单一符号对 `⟦ ⟧`

**决策:只定义一对符号 `⟦ ⟧`(U+27E6 / U+27E7),全部语义由 body 的微语法区分。**

- 罕见,几乎不与对话正文冲突;成对、左右可区分,天然支持流式状态机。
- **拒绝多对符号方案**(加粗一对、注入一对、……):符号膨胀、模型记忆负担、前端要多套解析。
- 拒绝 `{{ }}`(Jinja/SillyTavern 风格):与未来 Context Compiler 自身的模板语法冲突,且与正文冲突概率高;拒绝 `<< >>`(代码/HTML 冲突)。
- **加固**:系统 prompt 静态层注入独立 `inline_syntax` 片段(示例 + 规则),模型从上下文习得符号,保证可靠复现。解析失败时静默替换(见 §4.3),残次符号永不到达用户。

### 4.2 语法

```
placeholder := "⟦" body "⟧"
body        := injection | directive
injection   := path                  # 例:⟦time⟧ ⟦world:time⟧ ⟦char:name⟧ ⟦quest:name⟧
directive   := kind ":" payload     # 例:⟦b:这是粗体⟧ ⟦i:斜体⟧
path        := name (":" arg)*      # 首段为提供者命名空间:time / world / char / rand / game…
kind        := "b" | "i"            # V1 仅粗体 / 斜体
```

- **V1 无嵌套、无递归解析**:第一个 `⟧` 即闭合;解析结果不再二次解析(防环)。
- 转义:`\⟦` `\⟧` 渲染为字面符号。
- 含 `:` 的 body 按指令解析,不含 `:` 的 body 按注入路径解析;**无法解析(未知 kind / 未注册路径)→ 静默替换为空,不输出**。

### 4.3 流式解析规则(截断:用户永不看见原始符号)

StreamParser 是增量状态机,四个状态(plain / buffering / directive-open / done),核心规则:

| 状态 | 行为 |
|------|------|
| plain,无占位符 | delta 原样透传 |
| 遇 `⟦` | **进入缓冲:缓冲期内任何原始内容一律不发** |
| 指令 `⟦b:` | 风格在开标签即知 → 内容**渐进透传**(`text.delta` 带 `style=bold`),闭合 `⟧` 吞掉;流中途断开时已发内容保留,无原始符号泄漏 |
| 注入 `⟦time⟧` | 缓冲至 `⟧`(注入通常极短,截断窗口极小)→ 解释器解析 → 解析值作为单次 delta 发出 |
| 缓冲超上限(512 字符) | 视为模型失控,静默丢弃不输出 |
| 流结束仍有未闭合占位符 | 静默丢弃不输出 |

**静默替换原则**:解析失败 → 替换为空,不输出任何内容。解析层是协议层,不预设哪些程序存在——程序接入即解析成功,未接入即静默;残次符号永不到达用户。

### 4.4 注入解释器:InterpreterRegistry

- **解析层不认识任何程序。** StreamParser 只按协议认语法(`⟦ ⟧` 文法),注入应答通过上层注入的 `resolve(path, ctx) -> str | None` 完成;返回 None(程序未接入 / 路径未知)= 静默替换为空。程序接入即解析成功,解析层零改动——它只负责「LLM 与程序之间的沟通协议」。
- `path` 首段是**提供者命名空间**,注册表把「能做什么」预先固定:LLM 只能**指名**已注册路径,不能发明执行逻辑。
  - `⟦time⟧` → 系统时间;`⟦world:time⟧` → 世界时间(eidolon-world);`⟦char:name⟧` → 角色名。
- **占位符是注册表查询,不是 eval**。LLM 输出不可信,允许任意代码执行等于注入漏洞——这是安全边界,不是能力妥协。
- **注入 ≠ 函数调用。** 注册表 resolver 必须满足三条硬约束,违反者一律改为工具:
  1. **纯只读**:无副作用、不改变任何状态(世界 / 会话 / 上下文);
  2. **参数简单**:至多简单标量参数,无结构化入参(结构化参数属于工具);
  3. **确定性替换**:值是「引用」(当前时间、角色名、任务名),模型拿到即插入,不需要基于值再推理——需要推理的(如「查天气后决定怎么说」)走 tool call。
- resolver 签名 `resolve(path, ctx) -> str`,ctx 只读注入(会话/世界状态视图)。V1 解析结果限定纯文本;富媒体注入(如 `⟦asset:portrait⟧` 解析为图片段)是 V2 扩展点,协议上 `style`/segment 结构已兼容。

### 4.5 职责边界:何时占位符、何时工具

> **函数调用(有副作用 / 结构化参数 / 需校验 / 需模型推理结果)→ 一律 tool calls;占位符只做样式指令与只读值注入。**

| 场景 | 通道 | 理由 |
|------|------|------|
| 样式(粗体 / 斜体) | 占位符指令 | 表现层标记,与程序无关 |
| 句中引用确定值(时间 / 角色名 / 任务名) | 占位符注入 | 内联在句中、单轮完成、盲替换保证逐字精确 |
| 精确值不可被复述(系统生成的名词 / 数值) | 占位符注入 | 模型复述会失真;替换保证逐字正确 |
| 有副作用(开门 / 使用物品 / 世界变化) | tool call | 需程序验证,对齐「程序决定真实性」 |
| 结构化参数且需校验(天气 {location}) | tool call | JSON Schema 校验(strict 模式) |
| 需模型基于结果推理再表达(天气 → 怎么说) | tool call | 结果回流上下文,模型再表达,而非盲替换 |

## 5. Agent 事件内循环(AgentLoop)

一次 `chat` = 一个内循环;每一轮 LLM 输出先化为事件,再决定下一步:

```
turn = 1
transient = []                     # 循环局部消息(不入历史)
while turn <= max_turns:
    emit loop.turn{turn}
    chunks = gateway.complete_stream(messages + tools)
    events = StreamParser(chunks)  # text.delta / tool.call
    if 无 tool_call:
        解析后最终文本 → ConversationBuffer;emit chat.done;break
    for call in tool_calls:
        emit tool.call → ToolRegistry.execute(call)
        emit tool.result(ok/error)
    transient += [assistant(tool_calls), tool 结果消息]
    turn += 1
else:                              # max_turns 耗尽
    force_final: 追加一条「停止调用工具,直接回复」的指令,
    再做一次无工具调用收尾,保证用户一定拿到成段文本
```

### 5.1 终止条件

| 条件 | 行为 |
|------|------|
| 本轮无 tool_call 且有文本 | 正常结束 |
| `max_turns`(默认 8)耗尽 | 强制一次无工具收尾调用(`force_final`);再失败则 `chat.error` |
| 连接断开 | 循环在下一检查点退出(见 §7) |

### 5.2 上下文处理:transient 与缓存友好布局

- 工具调用/结果消息是**循环局部(transient)消息**,不进 `ConversationBuffer`——用户可见历史没有工具痕迹;最终只有模型**解析后的成段文本**入历史(值已替换、样式剥离;原文丢弃——语法习得由静态层 `inline_syntax` 片段承担,历史无需再示范)。
- **入史即固定**:轮末一次性写入,此后永不修改、永不重解析。任何在编译期重解析历史的做法都会让内容漂移(`⟦time⟧` 每次编译值都变)并击穿前缀缓存——禁止。
- 编译时 transient 拼接在尾部(每轮都变,位于变化区末端)→ 前缀缓存命中不受内循环影响,与 llm-gateway-and-context.md §3.3 布局原则一致。
- 工具执行失败**不回滚世界**,而是把错误文本作为 tool 结果回流——模型自行挽救(「门打不开,换种说法」)。对齐 4.3 错误回滚思想:上下文一致性由 loop 保证,程序真实性不因表达失败而撤销。

### 5.3 工具注册

- `ToolRegistry`:name → executor(同步/异步);`ToolSpec{name, description, parameters(JSON Schema)}` 随 `LLMRequest.tools` 下发(provider 原生 function calling,不写进 prompt)。
- 与 DeepSeek 协议对齐(OpenAI 兼容格式):模型返回 `message.tool_calls[{id, function:{name, arguments}}]`,执行结果以 `{"role": "tool", "tool_call_id": id, "content": ...}` 回流——正是 §5.2 的 transient 消息;函数体由本引擎执行,模型只发请求不执行。
- strict 模式(DeepSeek Beta:`base_url=/beta` + `strict: true` + `additionalProperties: false` 等)作为可选加固:V1 先按标准 JSON Schema 定义工具,稳定后按服务端要求收敛 schema。
- 每个生成器声明自己的工具允许集(dialogue 暴露什么工具、未来 environment 暴露什么工具,互不污染)。V1 注册表可为空——协议与循环先行,工具逐步接入。

## 6. 分层落点

| 层 | 改动 |
|----|------|
| `runtime/llm/`(服务层) | 新增 `chat_stream(messages, tools=None) -> Iterator[ProviderChunk]`(文本 delta / tool_call delta / finish);同步 `chat()` 契约不变;思考模式 `reasoning_content` delta 不进入渲染(按配置丢弃或调试用) |
| `runtime/llm_gateway/` | `LLMRequest.tools`;`ToolCall` / `LLMStreamEvent`;`stream_events()` 真流式事件流(组装 tool_call delta);`complete_stream()` 兼容接口(仅文本分块) |
| **新增** `runtime/inline/` | `StreamParser`(增量状态机)+ `InterpreterRegistry`(命名空间解析器) |
| **新增** `runtime/agent_loop.py` | `AgentLoop`:内循环编排(LLM ↔ 工具,事件产出) |
| **新增** `runtime/tools/` | `ToolRegistry` + `ToolSpec` + executor 契约 |
| `runtime/context/` | `compile(transient=...)` 尾部拼接;`ConversationBuffer` 不变 |
| `runtime/generators/` | `Generator.generate_stream(payload) -> Iterator[Event]`(同步迭代器,默认抛未实现,不破坏同步契约) |
| `backend/main.py` | `POST /api/chat/stream`(SSE + `asyncio.Queue` 桥接);旧 `/api/chat` 保留 |
| `frontend/` | `EventSource` 消费 + segments 渲染器 + 工具状态条 |

异步模型:同步 LLM 客户端在 worker 线程跑循环,事件经 `asyncio.Queue` 进 `StreamingResponse`;断连即向循环投取消信号。

## 7. 取消语义

- 前端断开(用户取消/关闭页面)→ 后端检测 → AgentLoop 在下一检查点(每轮边界、每个工具执行前)退出。
- **已执行工具的世界效果不回滚**(程序真实性:已发生的世界变化是事实);回滚只作用于上下文——transient 快照回退到本轮开始前。
- 未闭合占位符按 §4.3 失败开放处理。

## 8. 决策汇总

| 决策 | 选择 | 被否 |
|------|------|------|
| 传输 | SSE,事件信封与传输无关 | WebSocket(V1 无双向需求,留升级路径) |
| 占位符符号 | 单一符号对 `⟦ ⟧` + body 微语法 | 多对符号;`{{ }}`(与编译层模板冲突);`<< >>` |
| 解析位置 | 后端(引擎内),前端只见 segments | 前端解析(前端须见原始符号、且无法访问世界状态) |
| 注入语义 | 注册表查询(LLM 指名,不执行),仅纯只读、简单参数 | 任意代码执行(注入漏洞) |
| 函数调用语义 | 一律 tool calls(副作用 / 结构化参数 / 需推理) | 占位符触发副作用函数(无校验、循环外、不可见) |
| 解析失败 | 静默替换不输出(解析层不知程序,未接入即静默) | 失败开放渲染原文(残次符号可见) |
| 嵌套/递归 | V1 禁止 | — |
| 工具消息 | 循环局部 transient,尾部拼接,不入历史 | 写入 ConversationBuffer(污染用户可见历史) |
| 历史存储 | **解析后纯文本**(入史即固定,永不重解析);原文丢弃 | 存原文(重解析风险:值漂移、击穿缓存) |
| 内循环上限 | max_turns=8 + force_final 收尾 | 无限循环 / 无收尾 |
| 工具效果回滚 | 不回滚世界,错误回流模型 | 回滚程序状态 |

**原则:占位符与工具都是「输出中的程序接口」——引擎截获、解释、执行,用户只见渲染结果。协议先行,工具逐步接入。**
