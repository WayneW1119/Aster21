# Python 运行时结构重构与行为契约

日期：2026-10-23。本次在现有 Python 实现上调整组织边界，不实施 Rust 重写或新增 MCP。
前置分析见 [Codex 结构对照](codex-alignment-audit.md)。这里借鉴上游的服务、调用上下文和
工具执行边界，不宣称实现了上游全部接口。

## 边界

| 组件 | 本次职责 |
| --- | --- |
| Session | 输入队列和 Step 调度、状态、历史与持久化协调，决定关闭/暂停时机 |
| SessionServices | 持有发生器管理器与命令进程管理器，提供恢复源、暂停/关闭源、清理进程的入口 |
| StepContext | 继续固定本次请求的设置、客户端、Prompt、历史视图及工具绑定 |
| step.run_step | 记录输入、生成请求、校验和记录完整输出，交给 ToolRuntime 执行工具批次，最后检查预算提醒 |
| ToolInvocation | 原始调用、所属 StepContext、可选压缩边界；原始调用对外返回副本 |
| ToolCall | 在路由边界把 JSON 参数解析成命名调用；不改变错误校验顺序 |
| ToolRuntime | 顺序执行、请求上下文作用域、记忆版本提交、工具 trace、回执记录或入队 |
| ToolRouter / ToolResult | 选择已捕获 handler、转换原有可恢复异常、生成原有字符串回执 |
| session.tool_setup | 保持原有顺序组装内置工具，避免 Session 构造器继续累积注册细节 |

代码入口：
[services.py](../../agent_harness/core/session/services.py)、
[tool_setup.py](../../agent_harness/core/session/tool_setup.py)、
[context.py](../../agent_harness/core/tools/context.py)、
[runtime.py](../../agent_harness/core/tools/runtime.py)。

参考 Codex 的 `core/src/state/service.rs` 中 SessionServices 的职责边界、
`core/src/tools/context.rs` 的调用上下文和 `core/src/stream_events_utils.rs` 中工具运行时的组织。
具体本地接口适应自己的事件调度，不照搬 TurnContext。

## 保持的行为

1. 队列有事件才调用模型；每个 Step 一次性取走当前待处理事件。
2. 输入进入历史、更新 WorldState，然后捕获请求。请求期间到达的事件留到后续 Step。
3. 仍然收齐完整模型响应再执行工具，不引入流中派发、并行执行、抢占或重试。
4. 工具按原顺序执行；普通回执入队；非唤醒回执直接记录，均保留调用/回执配对。
5. 发送消息和等待的含义不变，无 turn、无 finish。普通 assistant 文本仍不直接发送给用户。
6. 记忆检查、版本证据、压缩时的 eligible range、历史视图和归档均使用原算法。
7. Prompt、工具名称、schema、工具排列、trace 事件和 rollout 格式不变。
8. 预期工具错误仍只捕获 KeyError/ValueError/TypeError/OSError，JSON 错误属于原有 ValueError；
   RuntimeError、取消和持久化等运行时故障继续向外传播，不伪造成功或补回执。
9. 关闭/暂停仍按原顺序处理发生器、输入队列、在途 Step、命令进程和持久检查点。
   普通 close 可排空已接受输入；suspend 在 Step 边界保存队列；不重放旧工具。
10. 原有连续 Step 设置与 100,000 输入 token 提醒保持不变，不增加强制压缩。

`Session.generator_manager`、`Session.process_manager` 继续返回同一服务对象，保留调用方访问。
原私有 `_history_view` / `_compact_boundary` 保留直接诊断和测试入口；实际运行时取值来自
ToolInvocation。工具参数和 handler 的 kwargs 接口不变。

## 调用上下文的作用域

ToolRuntime 使用每个实例独立的 ContextVar 绑定当前 ToolInvocation，退出时恢复。
同步 handler 经 asyncio.to_thread 执行时，能看到该调用的历史视图和压缩边界。
独立输入任务不会看到另一个任务的活动调用；错误或取消后作用域不会留在执行任务中。
模型能观察到的执行顺序与结果不变。

这为后续 MCP 留出接入边界：连接生命周期可归入 services，某个 Step 的目录与连接绑定
归入 StepContext，调用与错误通过工具运行时返回。当前未实现连接、注册或动态刷新。

## 重构前后对照

在修改运行时代码前保存了源代码快照，用同一驱动分别运行旧、新包。
驱动：[runtime_contract.py](../../tests/fixtures/runtime_contract.py)。
重构前输出压缩保存在 [runtime_contract_v1.json.gz](../../tests/fixtures/runtime_contract_v1.json.gz)，
不由新代码重新生成期望值。基线是 `54a6214` 加当时已完成的上下文预算提醒工作区，
不是裸 `54a6214`。

三组确定性场景：

- 主动记忆写入和补丁、读取与历史目录、范围 compact、唤醒/非唤醒发送、暂停、同 Thread 恢复。
- 工具执行期间外部事件插入、错误 JSON、未知工具、普通 assistant 文本与等待。
- token 阈值提醒及同档位去重。

比较完整 Prompt（包括固定指令、工具列表、有效历史和记忆基线）、trace、有效历史、归档、
rollout、待处理队列和最终状态。只规范化临时根路径、UUID 和运行时 ISO 时间；不忽略事件顺序、
工具内容、错误文本、版本证据或历史 ID。线程 ID 固定以保持目录 view_id 可比较。

重构前后规范化结果逐项一致。回归入口：

本次完整回归结果：387 项通过（包含 6 项新的调用作用域/错误边界检查和 1 项旧实现行为契约）。

```bash
python3 -m unittest discover -s tests -p test_runtime_contract.py -v
python3 -m unittest discover -s tests -p test_tool_runtime.py -v
python3 -m unittest discover -s tests -q
```

对照使用隔离的基线与重构环境；实验备份和原始运行产物不随项目分发。
若未来有意改变产品行为，需审查契约变更原因；不要因为断言失败而直接用新输出覆盖旧基线。

## 后续 Rust 版本

可以实现逻辑和功能等价的 Rust 版本。Rust 的对象、线程/任务和所有权组织不需要逐句复制
Python；应对齐上述行为契约、公开工具协议、历史投影与持久化规则。

现有契约驱动与结果可以作为起点，但不构成所有场景完全等价的证明。迁移时还需加入定时器、
程序发生器、命令副作用、取消、故障、部分补丁成功、旧 Thread 格式以及跨进程恢复的对照。
跨语言版本还应显式定义 JSON 错误文本和序列化兼容边界。

若目标是直接使用 Codex Rust 组件，仍需额外验证其 turn/扩展依赖如何接入我们的事件调度。
本次重构不会自动获得 Rust ABI 或 Codex rollout 兼容性。
