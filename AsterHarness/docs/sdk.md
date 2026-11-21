# Python SDK

SDK 和 CLI 使用同一个 Session、Thread、事件队列、工具路由与 WorldState。
它提供公开的 Python 入口，不是另一个 Agent loop，也没有 turn。
目前为 0.1 版基础 SDK，支持 Python 3.11+、Linux/POSIX；沙箱要求 Linux/WSL2、
bubblewrap 和 libseccomp。可通过 `pip install .` 安装，MCP/Code Mode 分别使用
`pip install '.[mcp]'`、`pip install '.[code-mode]'` 安装可选依赖。

产品名称为 Aster，主命令为 `aster`，Python 入口为 `from aster import Aster`。
原 `from agent_harness import Harness` 与旧命令、旧配置路径继续兼容。发行包 ID 暂保留
`aster-harness`，以便已有安装继续升级；GitHub 仓库地址不变。

## 从模板启动一个常驻 Agent

先按[配置管理](design/agent-configuration.md)创建并填写模板。SDK 直接使用相同的
`~/.aster/agents/` 和 `~/.aster/threads/`；已有安装兼容沿用 `~/.harness/`。
`ASTER_HOME` 和 `Aster(home=...)`
可以指定另一套数据。

```python
import asyncio
from aster import Aster

async def main():
    aster = Aster()
    agent = aster.from_template(
        "assistant",
        overrides={"context.warning_tokens": 80000},
        on_message=lambda text: print("Agent:", text),
        trace=True,
    )
    async with agent:
        await agent.send("开始维护这份资料。")
        await agent.emit("file_changed", {"path": "data.csv"})
        # 在这里接入自己的网页、消息队列或其他生产者。
        # wait 等待运行结束，不以一条消息作为结束条件。
        await agent.wait()

asyncio.run(main())
```

这个例子会持续运行；应用应在合适的时候离开上下文或调用 `await agent.pause()`。
可以运行交互示例，更方便地通过 `/quit` 正常暂停：

```bash
python examples/sdk/resident.py --template assistant
python examples/sdk/resident.py --resume THREAD_ID
```

`send()` 和 `emit()` 都只负责入队，不代表任务已经完成。发消息后立即退出上下文，
会正常暂停并保留未处理事件。Agent 输出一条消息后仍可继续工作，调用者也可随时插话。

## 工厂与配置

`Aster` 的工厂是同步方法，只做本机配置检查和 Thread 文件操作，不调用模型。
它们返回尚未启动的 `Agent`，并持有该 Thread 的写锁；必须正常关闭未启动的对象。

| 方法 | 用途 |
|---|---|
| `from_template(name_or_path, ...)` | 从模板创建独立 Thread |
| `from_config(path, ...)` | 从 TOML 文件创建独立 Thread |
| `create(workspace, model=... / client=..., ...)` | 不依赖现有 TOML，直接创建 |
| `resume(name_or_uuid, ...)` | 恢复正常暂停或显式校验恢复过的、带 `thread.toml` 的 Thread |
| `recover(name_or_uuid, reason=...)` | 校验完整失败 checkpoint，追加恢复审计记录，不启动 Agent |
| `threads()` | 列出 Thread、名称、运行状态 |
| `templates()` | 列出本机模板名称 |

`from_template` 和 `from_config` 支持 `name`、`workspace`、`overrides`。
覆盖使用 Python 原生值，键名与配置文件一致；字符串无需再加 TOML 引号：

```python
agent = aster.from_template(
    "assistant",
    name="researcher",
    workspace="/home/you/research",
    overrides={
        "model.name": "another-model",
        "context.warning_tokens": 90000,
        "sandbox.rules": [{"path": "/home/you/shared", "access": "write"}],
    },
)
```

工作目录须存在。配置文件中的相对路径相对于原配置文件；显式 `workspace` 参数
相对于调用进程的当前目录。创建后配置与 instruction 独立复制，修改模板不影响旧 Thread。

也可以完全用 Python 配置：

```python
from aster import Aster, ModelConfig, SessionConfiguration

agent = Aster().create(
    "/home/you/workspace",
    name="assistant-instance",
    model=ModelConfig(
        name="your-model",
        url="https://your-service.example/v1/responses",
        api_key_env="MY_MODEL_KEY",
        env_file="/home/you/.aster/credentials/model.env",
    ),
    instructions="长期维护工作区资料，按需与用户沟通。",
    configuration=SessionConfiguration(
        max_consecutive_steps=None,
        context_budget_warning_tokens=100000,
        filesystem_policy={"rules": [{"path": "/", "access": "write"}]},
    ),
)
```

Key 来自进程环境变量或 `env_file`，不序列化到 Thread 配置和 trace 中；进程环境优先。
工作区、相对权限路径和相对 Skills 路径按 `create` 的工作区解析。
SDK `create` 未显式传 `configuration` 时，默认沙箱开启、工作区可写、未匹配规则的路径禁止访问，
不限制连续 Step，100000 tokens 提醒。显式传入 `SessionConfiguration` 则沿用该对象
的全部设置（它作为底层配置类仍默认最多连续 12 Step、沙箱关闭）。

## 生命周期与恢复

```python
agent = aster.resume("researcher", on_message=print)
await agent.start()           # 等待核心运行时准备好，启动后台事件循环
try:
    await agent.send("继续之前的工作。")
    # 宿主继续做自己的工作，不需要阻塞等待一轮结束。
    await agent.wait()
finally:
    await agent.close()       # 与 pause 相同：正常暂停并保存
```

推荐使用 `async with agent` 自动 start/close。关闭时完成当前 Step，停止进程和事件
来源并保存待处理事件；不强行取消模型请求，不重新执行已完成工具。
活跃程序发生器遵循原有 resume 语义从入口恢复，保留状态目录；进程句柄和 Code Mode
临时数据不能恢复。单个句柄只用于一个 asyncio 事件循环，关闭后需要重新 `resume`，
同一 Thread 不能同时被两个句柄打开。

临时认证网络错误、传输中断及明确标记为过载、服务暂不可用或限流的模型流错误会产生 `model_retrying` trace，以 1、2、4 秒递增、最高 30 秒的基础间隔重试同一份模型请求。每份请求最多重试 5 次（包括初次请求共 6 次尝试），重试时间预算为 10 分钟，包含请求和等待所花时间。HTTP 或流内错误中的有效数值 `Retry-After` / `retry-after-ms` 可以延长等待，但不会扩大次数或时间预算；提示超过剩余预算时直接停止，不提前再次请求。等待期间仍可正常暂停。重试 trace 另记录 observed_at、elapsed_seconds、error_type、cause_type、context_type、http_status 和 network_reason_type；只记录异常类别及有效数值状态，不复制异常消息、URL、请求头或凭据。这些诊断不进入模型上下文。达到上限后明确报错，保存原 Thread，由宿主显式恢复，不自动重启 worker。输入不会重复写入，未成功完成的响应不会提交或执行工具，已完成工具不会重放。暂停期间的重试会保存继续处理事件，正常 resume 后继续。自定义客户端可在尚未提交任何输出或执行操作时抛出公开的 `RetryableModelError(message, retry_after_seconds=...)`；普通异常仍按致命错误处理。获取或解析身份验证公钥集失败（包括有效 JSON 中的空公钥集）只在完成严格校验前有限重试，轮换后的刷新响应留在受保护的暂存区，不重复兑换旧 refresh token。签名、有效期、issuer、audience、nonce 或身份不符仍拒绝激活，并只报告错误类别，不报告令牌或上游正文。额度耗尽、永久认证失败、上下文错误和原因不明的流内失败不会进入此重试。

HTTP 520（上游网关收到异常响应）从 0.1.14 起也使用同一有限重试机制。它不会扩大请求次数或时间预算，也不会把任意未知 HTTP 状态都视为临时故障。

SSE 读取还限制单次响应总时长为 10 分钟，在每次读行前检查，心跳不会重置期限；单次阻塞读取仍受 120 秒网络超时约束。该限制不通过取消线程并重开请求实现，避免留下后台请求或并发重试。自定义同步客户端仍须自行提供可返回的 I/O 超时。

已有失败 Thread 必须由宿主明确恢复：

```python
aster.recover("researcher", reason="已检查认证故障，确认失败 checkpoint 完整")
agent = aster.resume("researcher", on_message=print)
async with agent:
    await agent.emit("resume_requested", {"instruction": "根据已有历史继续未完成工作"})
    # 宿主按正常流程保持运行并接收消息。
```

`recover` 在持有独占文件锁时校验完整历史、checkpoint 和工具调用/回执配对，只追加 `session_recovered` 记录，保留原失败记录和 Thread ID。已恢复或正常关闭的 Thread 返回 `recovered=False`。运行中的 Thread、截断记录、缺少可验证运行边界或旧版完整轨迹、未完成的工具调用仍拒绝恢复；不会猜测或重放工具。恢复工厂本身不调用模型，需正常启动并投递继续事件或消费已保存的待处理事件。

模型和 instruction 在创建/恢复时捕获。修改 `agent.config_path`、`agent.instructions_path` 后，正常暂停，再 `aster.resume(...)` 应用。

文件系统策略另有公开的 `request_id = await agent.set_filesystem_policy(policy)`：传 rules 对象完整替换，传 None 关闭，在下一 Step 边界应用，不关闭 Agent。返回值只表示事件已持久入队。用 on_trace 的 event_enqueued 观察 source=runtime 的 filesystem_policy_applied / filesystem_policy_failed，其 data.request_id 与请求对应。同一步合并多项请求时最后一项生效，回执另列 superseded_request_ids。

这是运行时接口；使用 profile 管理的宿主还要把目标策略写入 thread.toml 的 sandbox 段，使下次 resume 按同一配置恢复。关闭时 enabled=false 且 rules=[]，宿主应另存想保留的规则。候选探针失败保持原策略，宿主相应恢复配置草稿；提交后的进程异常不能冒充明确拒绝。更新会清理旧权限进程、重连 MCP 和恢复 service 发生器，详见 [文件系统沙箱](design/filesystem-sandbox.md)。

## 异步接收消息

消息流适合异步网页、WebSocket 等接入。订阅时立即注册，从此只接收**新的**输出，
不会自动重放旧对话。请在投递消息之前订阅：

```python
async with agent.messages() as messages, agent:
    await agent.send("检查工作区。")
    async for message in messages:
        await websocket.send_json({
            "text": message.text,
            "thread": message.thread_id,
            "session": message.session_id,
        })
```

`Message` 是只读数据类。关闭 Agent 后，队列中已有输出仍可读完，再结束迭代。
可以建立多个独立订阅。每个订阅默认最多积压 256 条消息；`messages(max_queue=...)`
调整上限。慢消费者超过上限时，该订阅在已入队消息之后抛出 `MessageStreamError`，
不会阻塞模型、影响其他订阅或静默丢消息。离开订阅上下文会解除订阅。

SDK 不提供跨恢复的消息游标、网络确认或自动重试。需要可靠外部投递的应用，应自行
持久化收发状态；模型的发送回执不等于最终用户已经读到消息。

## 回调和运行记录

工厂方法还支持：

- `on_message(text)`：同步或异步回调，只处理 Agent 通过基础消息工具发送的输出。
- `on_trace(event, data)`：同步或异步观察回调，包含工具、请求、回复等运行记录，
  **不是**模型输入事件。
- `trace=True`：完整 trace 写入该 Thread 的 `traces/sdk-<session-id>.jsonl`，
  路径为 `agent.trace_path`。默认不额外记录 trace；rollout 始终持久化。

回调通过 SDK 的独立任务按队列顺序执行，模型线程产生的 trace 也会切回宿主事件循环。
回调应及时处理或交给自己的投递系统；高频完整 trace 可能产生大量回调数据。
回调异常由事件循环异常处理器报告，保留在 `agent.callback_errors`，不会终止模型。
`await agent.flush_callbacks()` 等待已排队回调完成并以 `CallbackError` 报告失败；
close 同样会等待并报告回调错误。不要在回调内部等待 start、close 或 flush，
这些生命周期操作由宿主所有者任务负责。

模型或工具调度的致命错误会通过 `agent.wait()`、消息流及 close 暴露，Thread 仍遵循
底层对失败或非正常关闭的恢复限制。取消或超时等待 `agent.wait()` 不会取消 Agent。

常用属性：`thread_id`、`session_id`、`workspace`、`status`、`is_closed`、`thread_path`、
`config_path`、`instructions_path`、`trace_path`。trace 不含 HTTP 认证头，但可能含完整
业务文件内容，默认关闭，不应提交到 Git。

## 外部事件、图片和自定义模型客户端

`await agent.emit(kind, json_data)` 返回持久事件 ID。它按普通宿主输入事件入队，
JSON 内的字段不会变成私有 WorldState 证据、权限更新或工具调用。
`send(text, images=[{"name": "photo.png", "data_url": "data:image/png;base64,..."}])`
沿用现有图片处理和附件存储。

`ModelConfig(protocol="responses", ...)` 选择 Responses 协议，旧配置省略该字段时默认使用 Responses。Harness 内部的模型边界为同步的 `generate(ModelRequest) -> ModelReply`。历史中的消息、工具调用、工具结果、工具发现与推理项按原顺序传入，回复经会话校验后写回原有 Thread 格式。

可以注入已有的同步 `ModelClient`，其 `respond(prompt)` 返回 `ModelResponse`。
模型调用仍由核心在线程中执行，不阻塞宿主事件循环。
公开导出了 `ModelClient`、`Prompt`、`ModelResponse`、`ResponseEvent`、
`ResponsesClient`、`ScriptedClient`，无需从 core 导入。

```python
agent = aster.create(workspace, client=my_client)
# 暂停后，Python 客户端对象不会序列化；恢复时再次提供。
resumed = aster.resume(agent.thread_id, client=my_client)
```

注入的 ResponsesClient 会复制连接设置后接入 SDK trace，不修改原客户端。
自定义客户端的 Python 对象、回调和订阅都需要恢复时由宿主重新提供。

离线示例会执行真实文件工具并验证恢复，不访问模型：

```bash
python examples/sdk/offline.py
```

### 异常停止与显式恢复

`Aster.recover(thread, reason=...)` 不调用模型或工具，也不删改旧记录。正常 failed-close 检查点仍按完整历史和工具回执配对校验。新版在模型请求前、完整工具批次结束后保存运行时边界；突然停止若处于这些可验证边界，可追加带原始归档哈希的恢复记录，恢复原 Thread 与待处理事件。恢复会话若只写入 session_opened 就中断，且此前存在完整、验证通过的正常关闭检查点，也可从该检查点恢复；只要打开后已有任何活动，就不能使用这条空会话恢复路径。边界后出现未完成工具或下一步骤的部分归档，拒绝恢复。输入队列、发生器进度或计时器截止时间无法确认时同样拒绝，不猜测业务结果。

旧版 Thread 没有边界检查点时，可由宿主显式选择 `recover(thread, reason=..., allow_trace=True)`。仅当该 Session 的完整 SDK trace 能证明停在未提交模型请求，队列与持久化事件逐项一致、有效历史完全相同，才允许恢复；轨迹缺失、截断或不匹配则拒绝。这是旧数据的证据桥接，默认恢复不依赖 debug trace。未结束的 once 来源标记失败、结果未知，绝不自动重放；service 来源仍按正常恢复规则从入口启动，外部副作用应由应用自身幂等处理。

模型响应流正常结束前断开、连接失败及已识别的临时服务错误，在尚未提交任何输出或工具时由 Session 对同一请求退避重试。配额不足、认证失败、TLS 验证失败和协议错误仍报错；不把永久失败无限重试。暂停等待当前模型请求/工具批次到安全边界，宿主不得把 HTTP 等待超时当成可以直接杀 worker 的依据。

## 宿主集成接口

aster.skills.builtin_directory() 返回本安装包的 skill 目录，validate_sources 校验宿主注册来源；aster.auth.ChatGPTAccount 提供宿主账号的配置与认证。aster.permissions.probe_windows_sandbox / smoke_windows_sandbox 提供有限的原生 Windows 后端检查。使用者不需要导入 core 或 experimental 模块。上述接口不创建团队、聊天账号或应用部署，不依赖外部业务项目。

普通 Thread 恢复及 SDK/CLI 恢复前的配置校验会建立临时的日志行位置索引，逐条读取与校验，避免把整份 rollout 同时展开到内存。索引不落盘，不修改日志格式、历史 ID、compact 溯源或有效上下文；完整历史仍可查询。显式异常恢复仍执行原有完整证据校验。

### Anthropic Messages

配置 `ModelConfig(protocol="messages", name="claude-haiku-5-5",
url="https://api.anthropic.com/v1/messages", api_key_env="ANTHROPIC_API_KEY")`。
使用 DeepSeek 的 Messages 端点时，模型名为 `deepseek-flash`，完整 URL 为
`https://api.deepseek.com/anthropic/v1/messages`，密钥取自对应的 DeepSeek 环境变量。
适配器请求 SSE，接收完整消息后才提交文本和工具调用；也能读取直接返回 JSON 的兼容端点。
