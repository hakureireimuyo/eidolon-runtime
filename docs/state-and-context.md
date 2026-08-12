# 状态模型、上下文管理与缓存

> 本文档记录**状态模型、动态上下文管理与缓存**方向的**最终决策**（已剔除"LLM 是状态机""状态重建""全量历史入模"等被推翻思路）。
> 相关文档：[运行时核心设计](./runtime-core-design.md) · [模型选型与本地验证](./model-selection-validation.md)（另见：剧情 / 世界 / 叙事引擎）

## 1. LLM 角色的根本转变

LLM 不是"人格和世界的生成器"，而是**高级自然语言推理与表达模块**。

- 人格 / 记忆 / 情绪 / 关系 / 世界状态 由运行时系统负责；
- LLM 只解决最后一层："在当前确定状态下，这个角色应该如何表达？"
- 模型能力与角色一致性的相关性因此显著降低（见 [模型选型与本地验证](./model-selection-validation.md)）。

## 2. 状态与上下文的分离（关键设计）

| 维度 | 性质 | 存放 |
|------|------|------|
| **State** | 机器可计算（数值参数 `trust=0.73`） | State Store |
| **Context** | 模型可理解（自然语言约束："角色已产生较高信任，交流更开放但仍谨慎"） | 进入 Model Input |

- 模型理解自然语言约束通常比理解裸参数更稳定。
- 状态经 Processor 转换为**行为约束**再进入 Context；State 与 Context 二者分离。

## 3. 状态演化，而非状态重建（确定性随机）

- ❌ 历史 → LLM → 猜测现在状态。
- ✅ 事件 → 状态转移函数 → 新状态。

随机性用数学规则描述，限制在社会 / 心理合理范围内：

```
trust(t+1) = trust(t) + 正事件*α − 负事件*β + noise(0~ε)
```

> **规则决定可能空间，随机性决定具体轨迹。** 人格稳定性 / 剧情连续性因此从 LLM 随机采样中被剥离。

## 4. 上下文按更新频率分层（动态上下文管理）

| 层级 | 更新频率 | 示例 | 缓存策略 |
|------|---------|------|---------|
| 静态层 | 几乎不变 | 世界观、角色基础人格、背景、规则 | 长期缓存，永久复用 |
| 低频层 | 天/月 | 季节、时间、社会环境、长期关系 | 周期刷新，增量更新 |
| 中频层 | 小时/天 | 当前任务、近期事件、关系状态、目标 | 状态缓存，事件触发更新 |
| 高频层 | 每轮/数轮 | 当前情绪、短期记忆、对话上下文、思考 | 局部更新，不重建全部 |

原则：LLM 每次看到的是"由多个状态层合成的当前世界快照"，不是完整重拼的 prompt。

## 5. 缓存友好的 Context Layout

- **越靠前越稳定、越靠后越动态**；变化只发生在尾部，以保持 token 前缀缓存可复用。
- 推荐布局（由稳到动）：

```
Runtime Contract → Extension Manifest → Character State → World State
→ Memory Retrieval → Conversation → User Input
```

- 不要把上下文更新理解为"替换"，而是"状态演化"——只更新变化部分，前缀不失效。

## 6. Context Compiler（取代 Prompt Builder）

不是简单拼接，而是"编译"过程：

```
Processor Output {facts, constraints, intentions, memories}
        ↓
Context IR（中间表示）
        ↓
Token Layout
        ↓
Model Input
```

Context IR 的价值：后续可独立决定"哪些进 prompt / 哪些进工具调用 / 哪些进隐藏状态 / 哪些进长期记忆"，而无需修改 Processor。

## 7. 历史 = 日志，状态 = 数据库（Event Sourcing）

- 对话历史适合保存"**事实发生过程**"（事件），不适合保存"**当前状态**"（`trust=0.73` 之类不应作为每条消息的 System 提示反复出现）。
- 闭环：

```
Events → State Transition Model → Character Runtime State → Context Builder
→ LLM → Response → Event Extraction → State Update
```

## 8. 多级缓存（数据库式）

| 级别 | 缓存内容 | 说明 |
|------|---------|------|
| L0 Prompt Template Cache | System/角色设定 | 角色不变则不重算 |
| L1 Semantic State Cache | 关系/目标 | 变化只改对应字段 |
| L2 Conversation KV Cache | System+State+PrevConv | 只追加新 User Message |
| L3 Memory Retrieval Cache | 按 situation fingerprint 缓存检索结果 | 相似情境直接复用 |

> 缓存本质是"可演化世界状态系统"的性能优化手段；真正核心是：**上下文 = 状态的投影**。
