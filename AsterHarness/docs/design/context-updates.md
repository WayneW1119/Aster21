# 参考 Codex 的上下文更新方式

参考上游 [OpenAI Codex](https://github.com/openai/codex)，提交 `c419fd01a70dd1d05356da443ac44328bda2e6f4`。
此文记录源码对应关系，不代表以下机制均已在本项目实现。

## 本项目已实现：WorldState 框架

代码位于 `agent_harness/core/context/world_state/`，沿用 Codex 的结构和命名：

- `WorldState` 汇集 section，提供 `render_full` 和 `render_history_diff`。
- `WorldStateSection` 是 section 协议，各 section 拥有自己的呈现策略。
- `WorldStateSnapshot` 保存 section 的比较基线。
- `WorldStateUpdate` 同时表达基线变化与需要追加的模型上下文 item；后者可以为空。
- 当前接入 `MemoryState` 和 `ToolsState`；MCP、自定义脚本和其他延迟工具的来源共用工具部分。
- 请求前缀、验证、观察和 compact 已统一通过 section 生命周期执行，详见 [WorldState 生命周期](world-state-lifecycle.md)。

我们的记忆由 Agent 主动编辑，变化已经在工具调用和回执中，因此 MemoryState 跟踪的是
独立记忆基线与已记录的记忆工具证据，保存其指纹和观察次数，不把磁盘当前内容当作模型已知状态。
失败与部分成功的回执也是证据，不能据此宣称整批文件已修改成功。

Session 初始化时恢复独立 MemoryProjection；旧 Thread 首次迁移时校验历史可还原的记忆与文件，
建立基线，仅从有效视图隐藏原记忆消息，保留归档。
每个 Step 把本次事件加入历史后，更新世界状态，再捕获 Prompt 与对应基线。
工具回执还在输入队列时不会提前推进基线；resume 恢复队列后按相同顺序处理。

基线以独立 `world_state` 记录持久化，并显示在 trace 中，不是 Responses 请求字段。
旧日志没有该记录时，可从独立记忆基线和工具历史重建，无需再次执行旧工具。
普通磁盘修改不自动刷新上下文；请求前的不变量校验会拒绝未登记的 index.md 变化。
MemoryState 的 world_state 指纹是观察元数据，不是完整记忆版本，也不是可见记忆基线。
事件驱动、Step 调度、Agent 自主记忆与工具协议保持原设计。

请求适配与此基线分开：DeepSeek 会拒绝工具调用与回执之间插入新用户/事件消息的请求。
本项目在 Responses 适配层把同批回执排在这些新消息之前，原始历史不变，WorldState
仍按真正进入历史的记忆证据更新。不会生成不存在的回执，也不会重新执行工具。
这项兼容处理由真实并发输入验收发现；并非声称 Codex 使用了完全相同的排序算法。

## 本项目已实现：记忆补丁

- `apply_memory_patch(patch)` 使用现有 Codex 风格的 `parse_patch` 与
  `derive_new_contents`，不是另一套补丁格式或字符串替换算法。
- 补丁中的路径相对于当前 Thread 的 `memory/`。支持 Add/Update/Move/Delete，
  限制为目录内的相对 Markdown 路径，禁止符号链接；主入口 `index.md` 不能删除或移动。
- 写入通过 `AgentContext.write_memory` 的临时文件、fsync 和原子替换完成，
  沿用 index 8000 字符、其他记忆文件 32000 字符的限制。文本修改保留行尾。
- 与现有工作区补丁工具一样，多文件操作不是事务；失败回执列出此前已完成的操作。
  Agent 应读取相关文件后决定如何继续，不能认为失败意味着所有操作都未生效。
- 工具调用参数记录补丁，结果返回 A/M/D 文件清单；index.md 实际改变时附完整新版本。
  普通修改不改写独立记忆基线。`read_memory` 提供当前状态，`write_memory` 提供完整写入，
  成功回执用版本号引用已有参数，避免重复正文。部分失败仍保留机器可读的 error 字段。

源码：`agent_harness/core/context_manager/agent_context.py`、
`agent_harness/core/tools/memory.py`、`agent_harness/apply_patch/`。

## Codex 的一般机制：WorldState

`codex-rs/core/src/context/world_state/mod.rs` 定义 `WorldState`、
`WorldStateSection`、`WorldStateSnapshot` 和 `WorldStateUpdate`。

初始上下文使用 `render_full`。正常运行比较已记录的基线与当前状态，通过
`render_history_diff` 生成更新片段；片段通过 Session 的历史记录入口追加。
新窗口/压缩替换历史时，重新构建初始上下文并建立新的状态基线。
部分路径还会检查相关上下文片段是否仍在有效历史中，缺失时重新声明。

入口：

- `core/src/session/mod.rs`：`record_context_updates_and_set_reference_context_item`、
  `build_initial_context_with_world_state`、`record_conversation_items`。
- `core/src/context_manager/history.rs`：`update_world_state`、`world_state_item`。
- `core/src/compact.rs`：`build_compaction_replacement_history`。

这些是相对于上游仓库 `codex-rs/` 的路径；本仓库不附带完整上游 checkout。

## 具体应用

| 状态 | Codex 源文件（core/src/context/world_state/ 下） | 模型可见的更新形式 |
| --- | --- | --- |
| AGENTS.md | `agents_md.rs` | 追加带“替换此前 AGENTS.md”说明的新指令；移除时明确声明旧指令不再适用。 |
| 环境 | `environment.rs` | 比较目录、shell、环境成员、日期、网络等状态，追加变化的环境上下文。 |
| 权限 | `permissions.rs` | 仅增加已批准命令前缀时追加新增前缀；其他变化可以重新追加完整权限说明。 |
| 协作模式 | `collaboration_mode.rs` | 模式/说明变化时追加新的模式上下文，未变则不重复。 |
| 模型说明 | `model.rs` | 普通窗口保留模型切换说明；增量工具窗口可以追加模型/基础说明更新而保留原前缀。 |
| 工具目录 | `top_level_tools.rs` | Responses Lite 的特定模式支持工具/命名空间增量声明、更新和移除；不是通用 Responses 能力。 |

“增量更新”不总是文本 diff：有的更新是字段增量，有的是一条声明覆盖旧版本的完整消息，
有的是专用工具声明 item。应根据状态类型选择，不能机械地把所有指令改成文本补丁。

此外，WorldState 的持久化状态也会记录完整快照或 merge patch；这是供 Harness 重建
状态的元数据，需与发送给模型的上下文片段区分。

## compact 衔接

已接入 Agent 主动范围压缩。最早更新链归并进独立记忆基线，后段更新归并成原位置快照，
整体 compact 重建当前基线。不会直接用最新磁盘内容覆盖局部选区对应版本。
基线与版本信息独立持久化，旧编辑链仍在 archive；后续 Step 重新计算 WorldState 指纹。
resume 恢复保存的记忆投影，不再次从磁盘刷新；见 [memory-projection.md](memory-projection.md)。
文件修改工具与上下文状态刷新是两个不同层面；见 [compact.md](compact.md)。
