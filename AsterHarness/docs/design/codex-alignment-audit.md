# Aster 与 Codex 核心结构对照

审查日期：2026-10-23。

审查对象：本项目 `54a6214` 基线及当前工作区已完成的上下文预算提醒变更；
参考上游仓库 `https://github.com/openai/codex`，提交
`c419fd01a70dd1d05356da443ac44328bda2e6f4`。
下面的上游链接固定到该提交，不代表其他版本或所有 feature 配置的行为。

此文保留审查时的状态；随后保持行为不变的结构调整见
[Python 运行时重构与行为契约](python-runtime-refactor.md)。
随后接入的基础 MCP 见 [MCP 设计与使用](mcp.md)，不回写下表的审查时状态。

## 结论

本项目是 **参考 Codex 组织方式的独立 Python 实现**，不是 Codex 的 Python 全量移植，
也不是换掉 turn 后其余代码均与 Codex 等价的版本。

已经有实质对应的边界：Session 管运行时；StepContext 固定一次请求的上下文和工具绑定；
ToolRegistry/ToolRouter 管工具；WorldState 生成上下文变化；ThreadManager 管创建与恢复。
但同名不等于接口兼容。底层 item 类型、流式执行、工具生命周期、异常体系、持久化格式、
配置和扩展系统仍有明显差异。

目前最有明确代码来源和差分场景证据的是 apply_patch 的部分算法适配；不能据此推断
整个运行时也是同等程度的移植。其他模块的具体历史来源，仅凭当前结构和注释无法全部追溯。

现阶段不建议仅为“语言一致”全盘改写成 Rust。应先确定目标是借鉴机制，还是持续直接复用
Codex 内部组件。前者可继续 Python 并收紧边界；后者值得单独验证以 Codex 为基底的 Rust 分支。
本文不是完整的性能审计，也没有给出无测量依据的迁移工期或兼容比例。

## 差异分类

- **设计差异**：来自本项目明确目标，迁移语言也应保留。
- **简化或缺失**：当前代码能力小于上游；不应笼统解释成事件驱动所必需。
- **兼容适配**：为现有服务或本地运行条件做的转换，需与核心语义隔离。
- **有限移植**：有明确源码来源，但实现范围受限，不能宣称全面等价。

一个模块可以同时具有多类差异。下表判断基于实际职责和控制流，不只依据文件名。

## 模块对照

| 模块 | 本项目入口 | Codex 对应入口 | 实际吻合程度与差异 |
| --- | --- | --- | --- |
| Session / Thread | [session.py](../../agent_harness/core/session/session.py)、[thread_manager.py](../../agent_harness/core/thread_manager.py) | [Session][cx-session]、[ThreadManager][cx-thread] | 职责边界对应。我们的 Session 直接持有调度、进程、发生器、记忆、压缩等服务；上游还拆分 SessionServices、ActiveTurn、配置解析和扩展。**设计差异＋简化**，不是结构体逐字段移植。 |
| StepContext | [step_context.py](../../agent_harness/core/session/step_context.py) | [step_context.rs][cx-step] | 实质对应：固定请求设置、工具定义与 handler 绑定，Prompt 返回副本。上游另外捕获模型、环境、MCP binding、AGENTS、取消令牌等。我们的冻结只覆盖已建模的字段；共享服务本身仍可变。**已对应核心不变量，能力简化**。 |
| 输入与调度 | [input_queue.py](../../agent_harness/core/session/input_queue.py)、[step.py](../../agent_harness/core/session/step.py) | [input_queue.rs][cx-input]、[turn.rs][cx-turn] | 我们把用户输入、普通工具回执和发生器通知统一排队，每个 Step drain；上游包含 turn 内输入、mailbox、steer 和 active-turn 协调。**明确的设计差异**；不能把我们的 queue 当成上游 queue 的等价实现。 |
| Prompt / item / response | [client_common.py](../../agent_harness/core/client_common.py) | [client_common.rs][cx-client-common]、[models.rs][cx-models] | Prompt、输入 items、工具 specs 等概念对应。我们主要使用任意 dict；上游使用 ResponseItem、ToolSpec、结构化输出 payload 等类型，并有更多媒体与上下文种类。**简化**；名称相同不形成数据接口兼容。 |
| 模型传输 | [client.py](../../agent_harness/core/client.py)、[sse.py](../../agent_harness/core/sse.py) | [client.rs][cx-client]、[responses_retry.rs][cx-retry] | 我们是同步 urllib 请求，通过线程避免阻塞事件入口；支持 SSE 解析和 JSON 兼容。上游有异步流、连接状态和传输恢复。我们的 adapter 明确无自动重试。**简化＋兼容适配**。 |
| 流式响应与工具执行 | [response_stream.py](../../agent_harness/core/response_stream.py)、[step.py](../../agent_harness/core/session/step.py) | [turn.rs][cx-turn]、[stream_events_utils.rs][cx-stream] | 我们收齐 response.completed 后提交整个 output，再按顺序执行工具；流中断时不执行部分响应里的工具。上游处理 OutputItemDone 时记录 item、安排 tool future，并管理在途调用；特定 hook 路径会推迟派发。**重要实现差异**，不是事件驱动必须如此。 |
| 工具注册与路由 | [registry.py](../../agent_harness/core/tools/registry.py)、[router.py](../../agent_harness/core/tools/router.py)、[result.py](../../agent_harness/core/tools/result.py) | [registry.rs][cx-registry]、[router.rs][cx-router]、[context.rs][cx-tool-context] | 定义、绑定、派发的职责对应。我们主要是 JSON kwargs 调 handler，结果为字符串＋wake_model；上游有 ToolInvocation、不同 payload、工具输出类型、取消、生命周期和扩展策略。**简化**；wake_model 是我们的调度设计。 |
| 命令与后台进程 | [process_manager.py](../../agent_harness/core/unified_exec/process_manager.py)、[process.py](../../agent_harness/core/unified_exec/process.py) | [process_manager.rs][cx-exec] | exec_command / write_stdin、进程句柄、PTY、有限输出收集的思路对应。我们只实现本地 POSIX 进程，不具备上游执行环境、exec-server、权限编排等整套结构。关闭时终止自有进程。**简化**。 |
| apply_patch | [apply_patch/](../../agent_harness/apply_patch)、[tools/apply_patch.py](../../agent_harness/core/tools/apply_patch.py) | `codex-rs/apply-patch/`，原适配来源见[归属说明](../../third_party/codex/README.md) | **有限移植**：解析、序列匹配和保留行尾的更新算法有明确来源，保留 25 个原场景。适配源提交是 `0144894…`，不同于本次对照版本。我们使用 JSON 参数、工作区路径限制；不含上游全部审批、流式解析、diff/undo 等能力。 |
| WorldState | [world_state/](../../agent_harness/core/context/world_state)、[Session 记录入口](../../agent_harness/core/session/session.py) | [world_state/mod.rs][cx-world]、[session/world_state.rs][cx-session-world] | section、snapshot、diff、历史追加和基线的组织方式实质对应。我们目前只有 MemoryState；上游包含模型、环境、工具、AGENTS、权限、插件等 section，还有 placement 与扩展贡献机制。**框架思想对应，接口和覆盖面简化**。 |
| Agent 记忆 | [agent_context.py](../../agent_harness/core/context_manager/agent_context.py)、[memory.py](../../agent_harness/core/context_manager/memory.py) | [memories/README.md][cx-memory]、[context/memory.rs][cx-memory-context] | 固定上下文与按需读取的思路可参考；我们的每 Thread 私有记忆、主动补丁、精确版本证据、压缩时刷新，是**自己的设计**。上游还有后台提取与全局整合流水线，不能把两者叫成同一套记忆实现。 |
| 历史与 compact | [history.py](../../agent_harness/core/context_manager/history.py)、[navigation.py](../../agent_harness/core/context_manager/navigation.py)、[tools/compact.py](../../agent_harness/core/tools/compact.py) | [context_manager/history.rs][cx-history]、[compact.rs][cx-compact] | 上下文历史管理和压缩概念对应。我们的完整归档、有效引用、稳定 h-ID、范围/嵌套来源查询、模型主动选范围摘要，是**自己的设计**；上游的压缩入口、窗口替换与恢复路径不同。不是上游 compact 算法的 Python 翻译。 |
| token 预算 | [context_budget.py](../../agent_harness/core/session/context_budget.py) | [context_window.rs][cx-window]、[compact.rs][cx-compact] | 我们只用已完成请求的服务端 input_tokens，达到配置阈值后发提醒；没有请求前估算和自动兜底压缩。**明确限定的第一阶段实现**，没有移植上游完整窗口控制。 |
| rollout / resume | [recorder.py](../../agent_harness/core/rollout/recorder.py)、[runtime_checkpoint.py](../../agent_harness/core/rollout/runtime_checkpoint.py) | [rollout recorder][cx-rollout]、[rollout_reconstruction.rs][cx-reconstruction] | 追加记录、恢复历史、区分运行时与持久 Thread 的思想对应。我们的 JSONL 类型、锁、事件检查点、压缩记录都是本地格式；只允许正常关闭后的恢复。上游有自己的模型和重建逻辑。**独立协议＋简化**；两边文件不可互读，也不保证任意崩溃恢复。 |
| 事件发生器 | [event_generators/](../../agent_harness/core/event_generators)、[events.py](../../agent_harness/events.py) | 没有把上游某个同名通用框架作为本项目实现来源 | timer、command completion、自编 Python 程序、管理器、队列限流与生命周期组成**本项目自己的通用事件机制**。上游有异步通知来源，但不应据此声称这套发生器管理器已与上游等价。 |
| 提示词、配置与扩展 | [prompt.md](../../agent_harness/core/prompt.md)、[session 指令](../../agent_harness/core/session)、[config.py](../../agent_harness/config.py) | [AGENTS 加载][cx-agents]、[core 依赖边界][cx-cargo] | 当前提示词按本项目行为编写，不能声称与上游逐字一致。我们只有有限配置和用户专属指令，没有完整 AGENTS 分层、Skills、MCP、插件和扩展注册体系。**自己的提示设计＋能力缺失**。 |

### 必须保留的行为

1. 用户可以随时插话；消息进入队列，不以一次回复定义工作的结束。
2. 工具回执和发生器事件驱动下一 Step；send_user_message 是普通工具。
3. Agent 私有记忆和精确版本证据；历史归档、有效投影及来源查询。
4. 发生器的注册、暂停、恢复和程序状态目录。
5. 兼容现有 Thread 的迁移方案；不重放已执行工具。

这些是产品语义，不应为了目录或语言一致而删除。

## 需要优先处理的简化

以下是建议的工程对齐目标，不代表本次已经修改或必须一次全部实现。

### 1. 工具调用边界比目录名更重要

当前 `Tool` 只有 spec、Python callable 和 run_inline，`ToolRouter` 解 JSON 后直接调用。
`run_step` 同时处理记忆特例、压缩边界、回执入队和执行记录。
加入 MCP 时若继续把连接管理和协议异常放进这些特例，会让 Session/Step 耦合继续增加。

建议借鉴上游 ToolInvocation / ToolOutput 的职责：明确调用身份、参数、已捕获 binding、
业务错误与运行时错误、输出和调度结果之间的边界。无需先做上游所有 hook/权限/工具类型；
但不要让 MCP handler 直接拥有或启动新的 Agent loop。

### 2. 请求快照应涵盖将来新增的服务绑定

当前 StepContext 已正确固定 router、specs 和 handler 引用，这是可以继续沿用的基础。
MCP 需要在快照中保留该目录对应的精确连接；刷新只影响后续 Step，不能把本次已展示的
工具临时路由到新连接。上游 [McpBinding][cx-mcp-binding] 是直接可借鉴的边界。
SDK 的异步连接和任务必须有明确 owner，Session 关闭后应完成清理。

### 3. 生命周期和错误语义仍需补齐

当前大多数模型传输失败会让 Session 进入 failed；异常类别和可恢复状态很少。
`ToolRouter` 只把有限的常见异常转换为回执，其他异常会向外传播。
这不等于一定有 bug，但接入第三方服务后需要明确 startup failure、tool error、
timeout、disconnect、cancel 各怎么处理，哪些能继续运行。

特别是网络超时不能解释成动作未发生：发消息等工具不能因未知结果自动重放。
Codex 的 retry 代码依赖自身的流式记录和在途工具模型，不能单独照抄重试循环。

### 4. 类型和适配需要逐步收紧

当前大量内部数据使用 `dict[str, Any]`，模型协议、历史和 trace 有时共享结构。
可先为新接入的配置、工具定义、连接状态和调用结果建立清晰类型；保留 SDK/Responses
转换在适配层。现有归档 item 的格式不应在没有迁移方案时整体替换。

`client.order_tool_outputs` 是兼容特定服务配对要求的请求投影；归档仍保持真实到达顺序。
这是有意隔离的适配，不能宣称该排序算法来自 Codex，也不应扩散进调度层。

### 5. 流式执行是单独决策，不是 MCP 的前置条件

当前“完整响应后执行”有明确、已经测试的失败边界。改成 OutputItemDone 后派发，
会改变部分响应持久化、取消、后续流失败和工具副作用的处理方式。
可以继续保留当前方式完成基础 MCP；若以后追求更低执行延迟，再单独设计和验证流式执行。
不要把这项变化当成语言重写附带的小优化。

## Python 与 Rust 的三条路线

| 路线 | 能获得什么 | 实际工作与成本 | 适用条件 |
| --- | --- | --- | --- |
| A：Python 中继续按 Codex 边界实现 | 继续利用当前运行时和测试；借鉴机制，协议通过 SDK 对接 | MCP 需适配器和生命周期管理；未来上游变动需要人工理解、改写和回归。不能直接链接 Rust 内部组件 | 主要目的是设计自己的 Agent，并选择性引入 Codex 机制 |
| B：把当前 Python 框架重写成 Rust | 更强的编译期约束，统一 Rust 生态 | Session、事件/发生器、进程、记忆、历史、持久化、CLI 和验证都要重写或迁移；如果接口仍是自己的，仍不能自然复用 Codex 内部组件 | 已有明确的运行时性能/部署需求，或明确决定维护自有 Rust 框架 |
| C：以 Codex Rust 源码为基底改造 | 最有机会保留和直接复用 Codex 已有组件 | 要切开 turn/active-turn/工具执行/恢复的耦合，再植入自有调度和记忆历史；需要维护 upstream fork 和兼容现有数据 | 主要目标是长期、大范围、持续直接复用 Codex 实现 |

路线 B 与 C 不是一回事。“改用 Rust”只统一语言；“以 Codex 为基底”还要求统一接口与依赖。

### 未来功能的实际复用成本

| 功能 | Python 路线 | Codex Rust 基底路线 |
| --- | --- | --- |
| 基础 stdio MCP | 使用 Python MCP SDK，仿连接管理器、工具适配器和 binding；无需重写主循环 | 可研究复用 codex-mcp / rmcp-client，但其配置、身份和运行环境依赖需要梳理；不是复制一个文件 |
| Skills / AGENTS | 改写发现、解析、选择与提示注入；按现有 Step 生命周期接入 | 可保留更多上游实现，但扩展贡献和 turn/step 的集成仍需适配 |
| 请求恢复 | 必须结合当前完整响应后提交的语义重新设计和测试 | 更有机会保留上游完整请求与工具执行链；若换掉该链，复用收益会减少 |
| 自有发生器、主动记忆、范围 compact | 可继续使用当前实现 | 需要移植本项目语义，并解决上游 context/history/rollout 的集成与数据迁移 |
| exec-server、多环境与复杂扩展 | Python 适配范围可能明显扩大；可研究稳定进程协议边界 | 更可能受益于上游生态，但需要接受或适配其整套配置与运行时边界 |

上游 [codex-core/Cargo.toml][cx-cargo] 直接依赖 protocol、history、config、mcp、rollout、
extension-api、exec-server 等多个 crate；Session/StepContext 的大量字段是 `pub(crate)`。
因此“Rust 更方便直接复用”是有条件的，不代表这些内部对象已经是可替换调度器的公共 API。

## 建议与决策门槛

建议短期选 A：保持 Python，基础 MCP 按上述边界实现，保留现有事件语义。
这次审查不支持“为了 MCP 必须先迁移 Rust”的结论，也没有性能测量能证明语言是当前瓶颈。

如果用户的真正优先级是尽可能直接复用整个 Codex，而不仅是参考其设计，建议另开隔离的
路线 C 技术验证：先用上游真实组件完成一次 MCP 调用，再验证如何不依赖原 turn 调度接入
一个自有事件驱动 Step。记录需要改动的 crate/API，而不是先全量翻译当前代码。

在迁移决策前，应至少验证：

1. 能否保留上游工具/MCP/模型调用链，又用我们的事件驱动决定何时进入下一次采样。
2. 自有 MemoryProjection、历史来源树和稳定 ID 如何表示与持久化。
3. 现有 Thread、发生器脚本、网站消息与暂停/resume 如何迁移，旧工具不被重放。
4. 后续跟进一次上游变更时，定制补丁会触及哪些模块。

若这些验证表明要重写上游大半条执行链，Rust 基底的直接复用收益就应重新评估。
反过来，若可以保留大部分执行链，仅替换清晰的调度入口，路线 C 的价值会明显提高。

## 本次检查范围与验证

本次阅读了两边核心入口、数据结构、执行控制流、WorldState、历史/记忆、rollout、进程管理、
MCP binding 和依赖边界，以及本项目已有设计文档和补丁归属说明。
没有对所有 feature、所有 crate 和所有代码行做行为等价证明。

已有回归入口包括 `test_step_context.py`、`test_streaming.py`、`test_rollout.py`、
`test_runtime_resume.py`、`test_world_state.py`、`test_memory_projection.py`、`test_apply_patch.py`。
这些能验证本项目的不变量，不能替代与上游的差分测试。

本次只添加此审查文档和 README 导航，不修改运行时代码，不实施 Rust 迁移、不继续 MCP
开发，也不重启正式常驻实例。历史迁入记录中“当时未实现”及旧路径的描述是历史状态，
不能用来推断本次审查时的功能覆盖面。

[cx-session]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/session.rs
[cx-thread]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/thread_manager.rs
[cx-step]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/step_context.rs
[cx-input]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/input_queue.rs
[cx-turn]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/turn.rs
[cx-client-common]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/client_common.rs
[cx-models]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/protocol/src/models.rs
[cx-client]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/client.rs
[cx-retry]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/responses_retry.rs
[cx-stream]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/stream_events_utils.rs
[cx-registry]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/tools/registry.rs
[cx-router]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/tools/router.rs
[cx-tool-context]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/tools/context.rs
[cx-exec]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/unified_exec/process_manager.rs
[cx-world]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/context/world_state/mod.rs
[cx-session-world]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/world_state.rs
[cx-memory]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/memories/README.md
[cx-memory-context]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/context/memory.rs
[cx-history]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/context_manager/history.rs
[cx-compact]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/compact.rs
[cx-window]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/context_window.rs
[cx-rollout]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/rollout/src/recorder.rs
[cx-reconstruction]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/session/rollout_reconstruction.rs
[cx-agents]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/src/agents_md.rs
[cx-cargo]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/core/Cargo.toml
[cx-mcp-binding]: https://github.com/openai/codex/blob/c419fd01a70dd1d05356da443ac44328bda2e6f4/codex-rs/codex-mcp/src/binding.rs
