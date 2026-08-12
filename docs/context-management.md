# 上下文管理设计

> 本文档描述引擎运行时层中上下文编译器的设计——如何将系统状态编译为 LLM 可用的上下文,
> 以及如何通过分层缓存使状态的频繁更新不导致全文重建。

## 1. 核心命题

传统角色 AI 把"上下文"理解为 prompt 的拼接。每次对话,系统把角色设定、历史记录、记忆摘要拼在一起,全部塞给模型。

本系统的上下文管理遵循一个不同的前提:

> **上下文不是文本的拼接,而是状态的投影。**

系统维护内部状态(数值、关系、事件),上下文编译器负责把这些状态编译为模型可理解的自然语言约束。当状态发生变化时,只更新受影响的上下文片段,而非重建全文。

## 2. 上下文按更新频率分层

上下文的不同部分有不同的变化速率。把它们拆成独立层,每层各自管理:

| 层级 | 更新频率 | 典型内容 | 缓存策略 |
|------|---------|---------|---------|
| 静态层 | 几乎不变 | 世界观设定、角色基础人格、核心行为规则 | 长期缓存,永久复用 |
| 低频层 | 天/月级 | 季节、时间、社会环境、长期关系 | 周期刷新,增量更新 |
| 中频层 | 小时/天级 | 当前任务、近期事件、关系状态、角色目标 | 状态缓存,事件触发更新 |
| 高频层 | 每轮对话 | 当前情绪、短期记忆、对话上下文、思考状态 | 局部更新,不重建全部 |

LLM 每次看到的是由多个状态层合成的"当前世界快照",而不是每次重拼的全文。

## 3. 上下文的状态投影模型

角色当前状态由运行时系统中的各能力子项目维护,上下文编译器负责收集并投影:

```
ImmutableProfile          ← 静态层:personality / background / values
EnvironmentState          ← 低频层:current_time / season / world_events
RelationshipState         ← 中频层:trust / affection / conflict_status
MentalState               ← 中高频层:emotion / mood / motivation / stress
ConversationBuffer        ← 高频层:recent_messages / unresolved_topics / short_term_memory
```

编译为模型输入:

```
FinalContext = StaticPrompt + DynamicWorldState + CharacterState + RecentConversation + CurrentTask
```

各部分的更新机制不同——StaticPrompt 只在角色更换时重建,DynamicWorldState 按周期刷新,
MentalState 每轮更新但只改情绪字段,ConversationBuffer 追加最新一轮消息。

## 4. 状态演化,而非状态重建

状态变化是通过确定性的转移函数计算的,不是让 LLM 从历史中推测:

```
事件 → 状态转移函数 → 新状态
```

而非:

```
历史记录 → LLM → 猜测现在状态
```

例如 trust 参数的变化:

```
trust(t+1) = trust(t) + positive_event · α - negative_event · β + noise(0~ε)
```

- 正负事件按权重影响 trust；
- 噪声被限制在参数化范围内；
- 输出的新 trust 值直接更新状态,上下文只重新编译受影响的字段。

**规则决定可能空间,随机性决定具体轨迹。** 人格稳定性和剧情连续性因此从 LLM 随机采样中被剥离。

## 5. 上下文编译管线

上下文编译器不是简单的"拼接 prompt",而是一个编译过程:

```
所有 Processor 输出 { facts, constraints, intentions, memories }
        ↓
Context IR(中间表示)
        ↓
Token Layout(按稳定性排序)
        ↓
Model Input
```

**Context IR 的价值**:IR 是状态到最终自然语言的中间层。有了 IR 之后,可以独立决定:
- 哪些信息进入 prompt；
- 哪些进入工具调用参数；
- 哪些进入隐藏状态供后续推理；
- 哪些归档为长期记忆。

修改 Processor 输出逻辑不需要改动编译管线,反之亦然。

## 6. 缓存友好的上下文布局

LLM 推理引擎使用前缀缓存——如果 prompt 的前缀不变,缓存命中,推理加速。

因此上下文按"越靠前越稳定、越靠后越动态"排列:

```
System Contract → Character State → World State → Memory Retrieval → Conversation → User Input
     ↑                  ↑                ↑               ↑               ↑            ↑
   几乎不变           低频更新         中频更新       情境触发        每轮追加      本次新增
```

- 同一角色、多轮对话之间:System Contract + Character State 的前缀完全不变 → 缓存命中；
- 状态更新的影响只限于尾部变化部分。

## 7. 多级缓存

### L0 — Prompt Template Cache

角色基础设定、世界观描述、行为规则——只要角色不变,永远缓存、不重算。

### L1 — Semantic State Cache

关系参数、情绪值、当前目标等结构化状态。缓存为自然语言片段:
"角色对玩家保持戒备,但上次冲突已解决"。
变化时只修改对应字段对应的片段,不影响其余部分。

### L2 — Conversation KV Cache

如果推理框架支持 KV Cache(如 llama.cpp),可以保留 System + State + 历史对话的 KV 计算结果,
每次只追加新的 User Message,不需要重新计算全部前缀。

### L3 — Memory Retrieval Cache

长期记忆按情境检索。当检索条件(当前情境特征)不变时,检索结果可缓存复用:
相同"玩家道歉后试图修复关系"的情境 → 复用之前的记忆检索结果。

## 8. 闭环:从事件到上下文到事件

上下文管理不是单向的,它处于一个循环中:

```
Events → State Transition → Character State → Context Builder → LLM → Response
                                                    ↑                        ↓
                                                    └── Event Extraction ←──┘
```

- 用户消息、世界事件、时间流逝都是事件；
- 事件触发状态转移、产生新状态；
- 编译器将状态投影为上下文；
- LLM 生成回复；
- 回复中提取新事件(如"角色表达了愤怒"→ anger +0.2),反馈回状态系统。

这个闭环确保系统持续运转,不依赖外部定时触发。

## 9. 设计边界

上下文编译器**不负责**:
- 状态的内部表示——那是各能力子项目的职责；
- 模型的 API 调用——那是 LLM 适配层的职责；
- 记忆的存储和检索——那是记忆子项目的职责。

上下文编译器**只负责**:从各子项目收集当前状态输出,编译为模型可直接消费的上下文格式。
