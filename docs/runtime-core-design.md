# 运行时核心设计

> 本文档记录引擎运行时层的**最终架构决策**(已剔除"集中式大脑""Prompt Builder"等被推翻方案)。
> 相关文档:[引擎核心:上下文流动的管理者](./engine-core.md) · [状态模型、上下文管理与缓存](./state-and-context.md) · [多智能体与多模态系统](./multi-agent-multimodal.md) · [演化路线](./evolution-roadmap.md)(另见:核心架构哲学与项目定位、独立项目职责与能力边界;图运行时内核设计见 `eidolon-graph` 仓库 `docs/`)

## 1. 运行时层的项目结构

运行时层不是一个单一项目,而是**一个组合入口 + 多个能力子项目**:

```
runtime/
├── eidolon-runtime/         ← 组合层:Web 服务 + UI + Extension Registry + Context Compiler
├── eidolon-graph/           ← 图运行时内核:图模型 + 执行引擎 + 内置节点(§1.1)
├── eidolon-mind/            ← 独立项目:人格偏置矩阵(纯库,被 eidolon-runtime import)
├── eidolon-world/           ← 独立项目:世界规则运行(纯库,被 eidolon-runtime import)
└── eidolon-memory/          ← 独立项目:记忆压缩系统(纯库,被 eidolon-runtime import)
```

**eidolon-runtime 只做组合和 UI**。它不包含领域逻辑——人格建模归 eidolon-mind、世界推演归 eidolon-world、记忆管理归 eidolon-memory。各子项目独立发版,在 eidolon-runtime 中作为第三方库被 import 引用。

### 1.1 图运行时内核(eidolon-graph):编辑与运行共享的内核

图运行时的工程组织与既有资产模式有本质区别,记录如下:

- **为什么不是纯数据容器**:角色资产是纯数据,静态 schema 校验足够,编辑器与运行时共享 asset-types 即可;图资产不是——校验是语义性的(绑定存在性、交叉连线、类型兼容、就绪语义、屏蔽语义),编辑事务需要**执行**状态迁移(改连线→就绪重置、换实现→迁移函数),预览需要**真正运行**图。因此生产方(图编辑服务)与消费方(eidolon-runtime)**共享同一个图运行时内核**,而不是各自基于一份 schema 实现——否则编辑器一份校验器、运行时一份校验器,语义必然漂移。
- **编辑器内嵌引擎**:与 Unity / Unreal 编辑器直接运行引擎本体同构。eidolon-studio 调用的图编辑服务基于内核运行 headless 实例做预览、校验与编辑事务;预览是确定性可复现的(同步轮次 + RNG seed),编辑器天然是调试器。
- **内核纯度**:内核零第三方依赖,不含 LLM / 网络 / UI。节点由宿主注册——编辑器注入 stub 做预览,eidolon-runtime 注册 LLM 节点 / Context Compiler 节点 / 工具节点等真实实现;内核内置节点仅 Clock / Counter / Comparator / AND / OR / NOT / Switch / Latch / Timer 等逻辑元件,领域节点一律不进内核。
- **内核内部两层**:model(图模型 + 资产格式 + 静态校验 + 内核版本标记)与 engine(tick 执行、调度、快照/持久化、RNG、编辑事务与状态迁移);编辑事务 API 属于内核,编辑服务只是它的 UI;图资产记录写入时的内核版本,编辑/加载时比对。
- **稳定核心**即 Node / Port / Signal / State / Graph / Tick / Asset / Snapshot 八个概念;内核设计文档已随仓库迁移至 `eidolon-graph` 仓库 `docs/`(总纲 / 执行模型 / 端口绑定 / 节点类型 / 资产 / 持久化与编辑 / 工程组织);内核的落地顺序是"阶段零:最小验证闭环"(见 `eidolon-graph` 仓库 `docs/graph-kernel-engineering.md` 与 [演化路线](./evolution-roadmap.md) §4.0)。
- **依赖方式沿用既有约定**:git 源 + pin rev——eidolon-runtime 与图编辑服务(editor 侧,由 eidolon-studio 调用)分别 pin 同一个内核,monorepo 与独立 clone 一致,无路径耦合。

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
