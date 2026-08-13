# 运行时核心设计

> 本文档记录引擎运行时层的**最终架构决策**(已剔除"集中式大脑""Prompt Builder"等被推翻方案)。
> 相关文档:[引擎核心:上下文流动的管理者](./engine-core.md) · [状态模型、上下文管理与缓存](./state-and-context.md) · [多智能体与多模态系统](./multi-agent-multimodal.md)(另见:核心架构哲学与项目定位、独立项目职责与能力边界)

## 1. 运行时层的项目结构

运行时层不是一个单一项目,而是**一个组合入口 + 多个能力子项目**:

```
runtime/
├── eidolon-runtime/         ← 组合层:Web 服务 + UI + Extension Registry + Context Compiler
├── eidolon-mind/            ← 独立项目:人格偏置矩阵(纯库,被 eidolon-runtime import)
├── eidolon-world/           ← 独立项目:世界规则运行(纯库,被 eidolon-runtime import)
└── eidolon-memory/          ← 独立项目:记忆压缩系统(纯库,被 eidolon-runtime import)
```

**eidolon-runtime 只做组合和 UI**。它不包含领域逻辑——人格建模归 eidolon-mind、世界推演归 eidolon-world、记忆管理归 eidolon-memory。各子项目独立发版,在 eidolon-runtime 中作为第三方库被 import 引用。

## 2. eidolon-runtime 自身的职责

作为组合入口,它只持有最小的内核:

```
eidolon-runtime
├── Extension Registry    ← 发现、加载、管理子项目的生命周期
├── Context Compiler      ← 收集各子项目输出 → 编译为 LLM 输入
└── Web Service + UI      ← HTTP API + 前端界面
```

- **Extension Registry**:定义子项目必须满足的接口契约(类似 Processor 的 ABC),负责加载/卸载/版本协商。新增一种能力 = 新增一个子项目并注册,不动 eidolon-runtime。
- **Context Compiler**:从各子项目收集状态输出,编译为模型可用的上下文。这是"编译"而非"拼接"——中间经过 IR 表示后再决定哪些进 prompt、哪些进工具调用、哪些进长期记忆。
- **Web Service + UI**:对外暴露的可视化操作入口。

## 3. 子项目的职责:State / Event / Processor

能力实现全部落在各子项目中,遵循统一模式:

| 概念 | 含义 | 由谁实现 |
|------|------|---------|
| **State** | 系统当前状态 | 各子项目定义自己负责的状态域 |
| **Event** | 状态变化的原因 | 由事件源产生,Registry 分发 |
| **Processor** | 监听事件并修改状态的转换器 | 各子项目实现 |

子项目不直接互相调用,只通过共享的 State Store 和 Event Bus 通信。整体循环:Event → Registry 分发 → 各 Processor 读取/写入 State → Compiler 收集 → 生成输出。

## 4. 被推翻的架构(不再采用)

- ❌ 把所有能力塞进 eidolon-runtime 一个项目:`eidolon-runtime { Mind, World, Memory, … }`。新增能力要改主项目、重测全系统。
- ❌ "Prompt Builder" 简单拼接上下文。改为 Context Compiler + IR。
- ❌ eidolon-runtime 作为唯一的运行时内核,领域逻辑全部内聚。改为 eidolon-runtime 只做组合,能力独立为子项目。

## 5. 共享状态,而非共享 Prompt

- ❌ 流水线式:Agent A 生成文本 → Agent B 读文本 → Agent C 续处理。
- ✅ 状态图式:每个 Processor 读取自己需要的部分、写入自己负责的区域。

```
State Graph
Character ─┐
Emotion ──┼─ Relationship
World ───┘   └─ Memory
```

各子项目是状态转换器的集合,不是文本接力者。

## 6. 与 ECS 同构

| ECS | 本层 |
|-----|------|
| 实体 Entity | Character |
| 组件 Component | Personality / Memory / Emotion / Relationship / Inventory / Location |
| 系统 System | eidolon-mind / eidolon-world / eidolon-memory(各自为独立 System) |

实体不知道系统如何工作——与 State + Processor + Event 同构。关键在于:**System 是独立项目,各自发版**。

## 7. 与协议层的关系

- 运行时层是消费方,由 eidolon-runtime 按 type 标签路由到对应子项目的解析器。
- 组合层"领域无知"与协议层"协议无知"同构:稳定的部分进入核心(Registry + Compiler),不稳定的部分成为扩展(各能力子项目)。
