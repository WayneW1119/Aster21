# stdio / Streamable HTTP MCP、延迟工具发现与资源访问

提供配置驱动的本机和远程 MCP 客户端：初始化、分页发现工具、原生客户端工具搜索、延迟加载、
资源列表／模板／读取、错误回执、正常关闭与 Thread resume 重连。来源列表由 WorldState
提供，搜索定义属于普通历史，压缩覆盖后移除，需要时重新搜索。无 turn 的事件调度、完整响应后
串行执行工具与记忆语义保持不变。详见 [工具发现与上下文](mcp-discovery.md)。

## 使用

MCP 使用官方 Python SDK，作为可选依赖；不启用 MCP 时不导入该 SDK 或 HTTP 客户端。

```bash
python3 -m pip install '.[mcp]'
python3 -m aster --workspace examples \
  --mcp-config examples/mcp/config.toml \
  --prompt '调用 MCP 的 echo 工具，传入“你好”，检查结果后告诉我。'
```

此命令会调用已配置的真实模型。示例只提供 echo，不连接正式网站或移动端实例。

配置使用与 Codex 接近的 `[mcp_servers.<name>]` 表：

```toml
[mcp_servers.webchat]
command = "/absolute/path/to/python"
args = ["/absolute/path/to/webchat/mcp_server.py"]
cwd = "/absolute/path/to/workspace"
env_vars = ["WEBCHAT_TOKEN"]
enabled = true
required = false
startup_timeout_sec = 10
tool_timeout_sec = 60
enabled_tools = ["send_message", "set_presence"]
disabled_tools = ["set_presence"]
```

stdio 支持上述字段，不声称兼容 Codex 完整配置。`args` 是参数数组，不经过 shell；
cwd 默认 Session 工作区，相对 cwd 也从工作区解析，脚本参数的相对路径由最终 cwd 解析。
`command` 可以是绝对路径或 PATH 中的程序。配置文件必须仅含 mcp_servers 表。
环境采用 SDK 的基础环境，加 env_vars 明确列出的进程环境变量；不传递全部宿主环境，
也不传递发生器的实时事件 socket/state 通道。当前没有 literal env 配置字段。

配置保存在新 Thread 元数据中，resume 不需要再传文件；变量值在每次启动时重新读取，
不会由这个配置机制写进 rollout。命令和参数本身会保存，不应把秘密直接写进参数。
CLI 的 --mcp-config 只用于新 Thread；嵌入宿主可用 Session.set_mcp_servers 提交运行时
完整替换，Step 边界更新连接与来源。尚无模型服务管理工具、手机配置 UI 或服务端目录
变更通知监听。已应用配置和待处理请求随 Thread 持久化。
旧 Thread 没有 MCP 配置时按空配置恢复；默认无 MCP 的旧行为契约保持一致。
包含新 mcp_servers 元数据的 Thread 需要支持该字段的程序版本恢复。

嵌入方可以用 `SessionConfiguration(mcp_servers={...})` 提供同样的字典配置。
服务列表在 Session 构造时捕获；修改原字典不会替换活动连接。

## 按需运行无状态 stdio 服务

默认 `connection_mode = "persistent"` 保持连接。对于进程内不保存会话状态、
不依赖订阅或跨调用资源游标的 stdio 服务，可以显式设置：

```toml
[mcp_servers.images]
command = "image-tool"
connection_mode = "on_demand"
```

初始化时发现并缓存工具目录、说明与资源能力，随后关闭子进程。每次工具或资源操作
重新启动一个官方 SDK 会话，核对完整目录未变后执行一次，并在完成、失败或取消时关闭。
同一服务的操作串行执行；空闲不占用子进程内存，代价是每次调用的启动开销。
目录变化时阻止业务调用，需要宿主重新加载配置；业务调用失败不自动重放。

该模式保留调用方的文件沙箱和环境变量白名单。它不会使有状态服务变成无状态服务，
不支持 HTTP，不适合数据库会话、交互式终端或事件监听。事件发生器的生命周期不受影响。
模式随 Thread 配置保存，恢复后仍按需连接；省略字段的旧配置继续保持原有行为。

## 远程 Streamable HTTP

配置 `url` 而不是 `command`，例如 [examples/mcp/http.toml](../../examples/mcp/http.toml)：

```toml
[mcp_servers.remote]
url = "https://mcp.example.com/mcp"
bearer_token_env_var = "REMOTE_MCP_TOKEN"
http_headers = { "X-Project" = "example" }
env_http_headers = { "X-API-Key" = "REMOTE_MCP_KEY" }
required = true
startup_timeout_sec = 10
tool_timeout_sec = 60
```

Bearer、普通请求头和 API Key 按服务要求选择，不需要的条目删除。`command` 与 `url`
必须且只能选择一个；HTTP 不接受 stdio 的 args、cwd、env_vars。HTTP 仍支持 enabled、required、
超时以及 enabled_tools / disabled_tools。配置字段名称参考 Codex 的 StreamableHttp 分支。

`http_headers` 是随配置保存的字面量；涉及凭据时用 `bearer_token_env_var` 或
`env_http_headers` 引用环境变量。变量只在建立连接时解析，解析值不写入配置或 rollout；
缺失或空值会使连接失败，不退回匿名访问。环境请求头按名称不区分大小写，覆盖同名静态头。
单独 Bearer 字段不能再与 Authorization 请求头同时配置。请求头名和值会校验，SDK 管理的
会话、内容类型等协议头不能覆盖。stdio 原有保存格式保持不变。

使用标准 Streamable HTTP，支持 JSON 和 SSE 响应。提供完整端点 URL，不自动跟随重定向，
也不自动回退到旧版独立 HTTP+SSE。无 OAuth 登录、授权服务发现或刷新令牌。
HTTPX 使用默认 TLS 验证和系统代理环境；HTTP 可用于明确配置的内网／本机端点。

沿用相同的 owner task、初始化、分页发现、工具调用、资源读取和错误语义。正常关闭发送
DELETE 结束本客户端的 MCP session，不停止远程服务；关闭有时间界限，失败时结束本地连接。
超时或中途断连的工具调用视为结果可能未知，不自动重放业务调用。SDK 自身的 SSE 流维护
不等于重新执行工具。resume 建立新连接、重新读取凭据；只修改环境变量不会自动替换现有连接。

HTTP 与 stdio 可以同时配置，也可通过宿主 set_mcp_servers 在 Step 边界替换。
未变化配置复用原客户端，必需服务替换失败时保留原绑定；没有新增模型侧服务管理工具。
来源、延迟搜索与 compact 的处理对两种传输相同，已被 compact 移除的定义需要重新搜索。

实现使用可选 SDK v1.26 的 `streamable_http_client`，不升级到 SDK v2。
官方参考：[SDK v1.26](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)、
[MCP Streamable HTTP 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)。

## 与 Codex 的边界对照

参考源码固定为上游 Codex `c419fd01a70dd1d05356da443ac44328bda2e6f4`。

| 本项目 | Codex 对应职责 |
| --- | --- |
| [mcp/config.py](../../agent_harness/core/mcp/config.py) | config/mcp_types.rs：服务配置与超时、筛选 |
| [mcp/client.py](../../agent_harness/core/mcp/client.py) | rmcp-client：协议与传输适配；这里改用官方 Python SDK |
| [mcp/connection_manager.py](../../agent_harness/core/mcp/connection_manager.py) | codex-mcp/connection_manager.rs：连接初始化与目录管理 |
| [mcp/binding.py](../../agent_harness/core/mcp/binding.py) | codex-mcp/binding.rs：固定目录及精确客户端句柄 |
| [tools/mcp.py](../../agent_harness/core/tools/mcp.py) | core/tools/handlers/mcp.rs、mcp_tool_exposure.rs：模型工具定义与调用适配 |
| SessionServices / StepContext | 服务由 Session 持有，Step 捕获本次请求对应的 binding 和工具路由 |

这是按职责改写的 Python 子集，不是直接翻译全部 Rust 对象或引入 Codex 内部 ABI。
未连接时不存在运行中工具；首次驱动 Session 时初始化 MCP，在第一个模型请求前安装目录。
服务按配置名称、工具按原始名称稳定排序；先应用 enabled_tools，再应用 disabled_tools。
业务工具不会在顶层 tools 中直接展开。搜索结果以 `mcp__服务名` 命名空间和函数名提供
完整定义，后续 function_call 带 namespace；执行仍使用服务原始工具名。内部保留原有
`mcp__服务名__工具名` 规范化规则作为路由键，特殊／超长名称使用稳定哈希。

连接、工具列表与转换后的定义一起捕获，handler 绑定具体客户端，不在调用时通过名字
寻找可能已替换的连接。每个 Step 继续使用其已捕获路由，模型看到的 schema 和执行绑定一致。
本阶段仅首次发现目录，不处理 server 的 tools/list_changed 通知。

## 事件与错误

普通 MCP 结果保留 content、structuredContent、isError 等 SDK 返回字段，编码为 JSON 文本，
放进现有 function_call_output。回执入队，下一 Step 可见，不在当前响应中提前观察结果。
MCP 发消息工具是普通工具，仍会唤醒后续 Step，不意味着工作结束。
目前不提供 MCP 专属的非唤醒选项；内置 send_user_message 的选项保持原样。

服务端业务错误通过 isError 返回，作为模型可观察的工具结果。超时、连接或响应验证失败
也返回错误回执；如果调用可能已经送达，标记 error.outcome_unknown=true。
断连或超时不代表业务动作回滚，不自动重试、重连或重放。错误也不能证明消息一定没发出去。
再次 resume 只重建连接；旧工具调用和历史回执不会重新执行。

初始化失败的默认可选服务不提供工具，向队列追加一条 mcp_startup_failed 事件。
required=true 的服务失败则停止 Session，详细原因及 stderr 路径在 trace 中。
缺少可选 SDK 时启用 MCP 会给出明确安装错误；无 MCP 配置不需要 SDK。
分页检测重复 cursor、重复工具名，单服务最多 100 页和 1000 个工具，超出时启动失败。

stderr 不参与协议，保存到每次 Session 独立的日志中：持久 Thread 在
Thread 的 `mcp/<session_id>/`，普通 Session 在工作区 `.harness/mcp/<session_id>/`。
trace 有 mcp_startup 状态、模型请求和工具完整回执；没有复制环境变量值。
服务自行输出的日志与业务结果仍需按实际内容保管。

## 生命周期

SDK 的 stdio / HTTP transport、ClientSession 和 AnyIO 任务组由同一个 owner task 打开和关闭。
工具通过该 owner 的队列发起请求，避免跨任务退出 cancel scope。
正常暂停允许当前 Step 工具批次结束，然后关闭连接；stdio 同时清理本机子进程，HTTP 只结束客户端会话。取消时清理在途请求。
空队列 run_until_idle 返回时连接继续存活，直到 Session 关闭。
每个 Session 使用同一个 asyncio loop；不在新 loop 里复用旧 Session 的连接。

服务通过 stdio 工作不需要公网端口。网站收消息仍可用现有程序发生器；发送函数包成
MCP 工具即可，两条链路可以独立。普通 MCP server 不会自动变成拥有发生器通道的程序。

## 范围和验证

本阶段没有 OAuth、旧版独立 SSE、MCP prompts、模型侧服务管理工具、服务端目录通知监听、
自动故障重连、sampling、elicitation 或 MCP 任务扩展。MCP 图片已可转成原生视觉输入，
音频仍未接入。这些后续分别设计，不改变基础事件循环来模拟支持。

`tests/test_mcp_http.py` 使用真实 SDK HTTP 服务覆盖 JSON/SSE、Bearer 与请求头、资源读取、
认证失败、超时、中途关闭、混合传输、替换回滚、DELETE 会话结束及 compact/resume 后重新搜索。

```bash
python3 -m unittest discover -s tests -p test_mcp.py -v
python3 -m unittest discover -s tests -p test_mcp_discovery.py -v
python3 -m unittest discover -s tests -p test_runtime_contract.py -v
python3 -m unittest discover -s tests -q
```

MCP 集成用确定性模型输出驱动真实 SDK 子进程、真实本机 HTTP 测试网站和普通事件回执。
覆盖分页、筛选、schema 快照、业务错误、超时、崩溃、初始化失败、环境转发、进程清理及
同 Thread resume 不重放；不调用付费模型，不修改正式 Agent 或其网站。
没有安装可选 SDK 时，配置检查继续执行，真实 MCP 集成明确标记跳过；CI 单独安装 extra 验证。

## 设计一：agent 自主管理命名 HTTP MCP 连接

宿主可授权 agent 调用 `connect_mcp(name, url, credential_ref?)`。连接过程遵守该 agent 的网络权限。公开服务可省略凭据，需要认证时通过私有引用提供，每条连接仅使用自己的凭据。

agent 自选的连接名在当前 Thread 内唯一，也是该连接的工具命名空间。允许多个名字指向同一地址，每条连接可使用不同凭据。创建时若与已有连接（包括宿主配置的连接）重名，返回错误并保留原连接。连接配置随 Thread 保存，断线重连和恢复时沿用原名。

agent 可以列出连接，断开自己添加的连接。宿主继续管理预置连接和本地 stdio 服务。程序发生器可以独立于 MCP 连接运行。

## 套件、连接与发生器

- 每个 agent 对同一套件只安装一份。保存参数和启用状态，停用后可重新启用。宿主管理套件内的 MCP、skill 来源和发生器；agent 自建资源单独管理。
- 用户可向 agent 交付服务地址和账号连接资料，或交付宿主保存的私有凭据引用。聊天中直接发送的秘密可能进入历史。绑定凭据时限定可用目标；注册连接与监听可以复用同一份授权资料。
- 套件可声明 HTTP 或 stdio MCP。浏览器类套件可提供受管 stdio 配置及 skill；skill 也可解释如何在现有权限下使用另一套配置运行独立客户端和子进程。将额外 stdio 服务注册进 Thread 工具目录是不同的能力；第一版自主连接入口的范围为 HTTP。
- 发生器支持人可读的名称和可选备注，操作使用稳定的 generator_id。名称和备注随注册及检查点保存；旧注册没有这些字段也应能恢复。新增发生器 WorldState 区域，以当前有效状态和净变化进入上下文，沿用已有的压缩与恢复机制。单纯的配置变化不触发模型请求；来源自身的业务事件仍可唤醒 agent。
- 套件声明所需权限，宿主决定是否授予；停用套件保留独立权限配置。恢复时以当前安装状态为准。既有部署中的名称、资源归属和接管方式需逐项核实，再决定迁移。

### 宿主使用套件

目录入口是 `suite.toml`。例如以下套件加载旁边的 skill、连接一个 HTTP MCP，并注册一个定时器。参数由安装方提供，替换只发生在解析后的 TOML 字符串中。

```toml
id = "research"
skills = [{ path = "skills" }]
requires = ["research-feed"]

[mcp.feed]
url = "${endpoint}"
bearer_token_env_var = "RESEARCH_FEED_TOKEN"

[generators.review]
kind = "timer"
name = "Review"
notes = "Check open tasks"
delay_ms = 3600000
interval_ms = 3600000
repeat_count = 0
message = "Review outstanding tasks"
```

原生 SDK 可以在启动前或运行期间调用 `configure_capabilities(grants=["research-feed"], allow_connect=True)`、`install_suite(path, parameters={"endpoint": ...})`、`list_suites()`、`set_suite_enabled("research", False)` 和 `remove_suite("research")`。停止的 Thread 也可以用 `aster suite install THREAD DIRECTORY --param endpoint=URL` 或 `aster suite list|enable|disable|remove` 管理。需要权限标签的套件须先由宿主设置 grants。标签表达宿主已核准的条件，不能修改 OS 的沙箱或提权。原生 stdio MCP 沿用已有的沙箱启动方式。程序发生器支持 kind="program"、script_path、args、mode、name 和 notes。

安装时解析并保存配置，资源文件留在安装路径。Thread 私有目录中的 capabilities.json 管理期望状态；Rollout 保存已发生的事件与发生器检查点。恢复时对齐两者。同一套件内的发生器具有稳定资源标识；已完成的一次性发生器在普通恢复时不会重跑。关闭再开启会重新注册并开始新的周期。安装器不会重新投递队列中的业务事件。关闭自主管理权限会停用自有 HTTP 连接，恢复权限后重新接入。

为专用 HTTP MCP 绑定宿主环境变量可传 `credentials={"feed": {"url": endpoint, "env": "FEED_TOKEN"}}`。成员也可以传自己可读的凭据文件绝对路径。文件中保存 mcp_url 和 token，必须精确匹配目标端点。连接配置保存引用；令牌在打开 HTTP 连接时读取。旧 Thread 的自主连接权限默认关闭。
