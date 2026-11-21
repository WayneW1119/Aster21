# WorldState 的统一生命周期

WorldState 管理模型应持续了解的上下文状态。各部分保留自己的内容、角色和协议，统一的是基线呈现、历史更新、验证、压缩和恢复的入口。已接入记忆、skills、环境权限与工具部分；各部分共用生命周期，没有新增计划运行机制。

## 请求布局

```text
instructions：通用行为、运行时规则、Agent 指令
input：
  按注册顺序呈现各 section 的固定基线
    memory：固定的 index.md 版本
    skills：本地操作指南的名称、简介、路径和内容摘要
    tools：延迟工具来源简介
    environment：有效沙箱权限与工作区信息
    宿主接入的其他 section
  有效历史
    用户输入、模型输出、工具调用与回执（包括原生工具搜索结果）
    后续 section 更新
    compact 摘要与必要的精确状态检查点
```

工具 API 定义仍在原生 `tools` 字段，延迟定义仍经原生 `tool_search_output` 加载。WorldState 不是把所有工具协议都改成普通文本。MCP、自定义脚本和其他延迟工具来源共用 `tools` section，目前没有单独的 MCP section。

基线不会每个 Step 根据当前状态重写。搜索定义只属于有效历史，compact 覆盖后移除，不搬入基线；来源简介保留，需要时重新搜索。普通更新进入历史；已有工具回执能完整承载更新时，不再重复发一条通知。内容的角色与格式由所属 section 决定，不强制统一为用户消息。

## 共同接口

`WorldStateSection` 描述协议，`ContextSection` 提供可选钩子的默认实现。现有 `MemoryState`、`SkillsState`、`ToolsState` 均使用这套生命周期：

| 方法 | 职责 |
| --- | --- |
| `prefix_items()` | 渲染固定基线，不从当前状态悄悄刷新前缀 |
| `render_diff(previous, items)` | 比较已记录状态与可见证据，返回元数据和需要追加的项 |
| `project_items(items)` | 将内部检查点转换成原生输入，移除内部元数据 |
| `validate(items)` | 检查基线与历史能否还原当前可见状态 |
| `observe(items)` | 观察已记录历史，返回需要持久化的投影记录 |
| `snapshot_state()` / `restore(state)` | 导出／恢复扩展部分的 JSON 投影状态；无独立状态时可不导出 |
| `plan_compaction(...)` | 无副作用地生成候选基线和位置检查点 |
| `validate_compaction(plan, items)` | 验证候选状态与替换后的上下文一致 |
| `commit_compaction(plan)` | 在完整操作持久化成功后应用候选状态 |

`WorldState.render_context()` 统一验证、按顺序渲染前缀并投影历史。`StepContext` 消费结果，负责指令和通信渠道的请求包装，不再分别拼接记忆、工具前缀。历史写入后的观察、压缩前的规划和压缩后的提交也通过 WorldState 遍历各部分。

需要区分三种数据：`WorldStateSnapshot` 是用于比较的观察元数据；各 section 的投影状态保存固定基线与当前状态；可见前缀和历史项才是实际发送内容。内部快照不作为额外 Responses 字段传给模型。

## Compact 与持久化

流程为：验证现状 → 收集所有 section 的候选方案 → 合并位置检查点 → 验证所有候选状态 → 持久化整个压缩操作 → 替换有效历史 → 提交各 section 的投影。候选验证或持久化失败时，不应用候选基线，也不替换有效历史。

整体压缩可将当前状态提升为新基线。局部压缩保留范围外的更新，必要时在原位置留下检查点。精确状态由 section 自己重建与验证，不从自然语言摘要猜测。

仅含现有记忆、工具部分时，继续写旧的 v1–v3 压缩格式，保持存档和请求兼容。出现扩展部分时，使用 v4 的 `world_state_sections` 映射，每部分包含 `state` 和 `checkpoints`。兼容旧字段的映射集中在存储适配处，Session 的压缩编排不再判断具体 section 类型。

## 接入后续部分

宿主可以在运行 Session 之前调用 `session.add_world_state_section(section)`；`memory`、`skills`、`tools`、`environment` 为内置保留 ID，重复 ID 会被拒绝。有独立状态的部分实现 `snapshot_state` 与 `restore`：初次注册会保存初始状态，后续变化由观察钩子保存。恢复 Thread 后，宿主重新提供实现对象，框架向它交付已保存的状态；尚未重新接入的有状态部分会阻止模型请求，避免静默丢失上下文。

这不会把 Python 对象或运行进程自动序列化。注册代码由宿主提供；有状态部分必须实现符合自身协议的验证和 compact 钩子。其他后续部分只需接入这套接口，不要求修改通用 Step 和压缩调度。

## 保持兼容的边界

Python 的 `Prompt.memory` 仍作为旧调用方及测试的兼容检查槽保留，由 `WorldStateContext.to_prompt()` 单点适配；section 生命周期使用的都是有序前缀和历史项。实际 Responses 输入顺序与原生格式不变。内置投影的创建和旧存档解析仍有各自的类型适配，这不等于统一其业务状态格式。

测试用第三部分仅验证扩展路径，不是新增产品功能。覆盖固定前缀、追加更新、局部／整体 compact、写盘失败、未接入部分的拒绝、正常 resume，以及记忆＋真实 stdio MCP＋第三部分共同压缩后保留来源、重新搜索再使用工具定义。旧运行时契约也继续逐项比较。
