# 多智能体与多模态系统

> 本文档记录"多智能体 / 多模态生成"方向的**最终决策**(已剔除过渡讨论)。它描述 eidolon-runtime / eidolon-mind 等未来层如何组织多个异构处理器。
> 相关文档:[运行时核心设计](./runtime-core-design.md) · [状态模型、上下文管理与缓存](./state-and-context.md) · [模型选型与本地验证](./model-selection-validation.md)

## 1. 从 Processor 到多 Agent

目标不止单角色对话,而是**多个异构智能处理器围绕共享状态协同演化**。例:

- 对话模型:负责语言
- 情绪模型:推理心情 → 驱动头像变化
- 视觉状态模型:根据情绪改变外观
- 记忆压缩模型:动态压缩记忆避免上下文过长
- 世界模型:推理世界状态与突发事件

LLM 是系统中的"**计算节点**",不是系统中心。

## 2. 多个 Agent 围绕共享状态

| Agent | 读取 | 写入 |
|-------|------|------|
| Dialogue Agent | character / emotion / relationship / memory | message |
| Emotion Agent | Event + State | Emotion State Update(如 `sadness:+0.3`)→ 头像系统监听并改外观 |
| Memory Agent | 事件 | 短期→重要性评估→长期记忆；回写 relationship 等状态 |
| World Agent | time / event | `weather_change` 等事件 → 影响角色 mood |

各 Agent 是**状态转换器**,不是流水线。

## 3. 生成模型是"昂贵行动者",由状态触发(非 LLM 决定)

- ❌ "对话模型判断该生成图"——会退化为 Prompt 驱动应用,惊喜感弱。
- ✅ 让**状态变化触发能力**:

```
用户离线3天 → World Agent 检测时间 → Relationship Agent 发现关系变化
→ Emotion Agent 推理 → Expression Agent 决定改视觉 → Image Agent 生成新状态图
```

- 决策层(Planner)按 `action / reason / priority / cost` 选择 文字 / 图片 / 动画 / 视频；Scheduler 判断用户是否在线、是否值得消耗、是否有缓存。
- **原则:生成模型应该是昂贵的行动者,不是持续运行的感知器。**

## 4. 生成内容是"状态的投影",不是最终数据

- 不保存 `avatar.png`,保存 `appearance_state{emotion, weather, lighting, clothing}`。
- 图片 / 视频 / 3D 都从该状态渲染；模型替换不影响数据层。

## 5. "意识 / 觉知层" Agent(可选方向)

不负责具体任务,只判断"**当前是否应主动做某事**":用户消失 7 天 → 角色应表现在意 → 提出 surprise 动作。这是从 Agent 到"**自主体**"的区别。

## 6. 用户看到的 vs 系统内部

用户看到一个角色,系统内部是一个小型生态(Cognitive / Creative / Physical Agents)。最大价值:AI 从"等待输入并生成输出的函数"变成"**持续运行、维护内部状态、根据环境变化采取行动的系统**"。
