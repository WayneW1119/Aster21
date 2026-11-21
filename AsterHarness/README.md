# Aster

> Windows 原生预览：见 [Windows 运行说明](WINDOWS.md)。Linux 仍走原有实现；Harness/Squad 的 Windows 阶段仅支持用户显式关闭文件沙箱的可信开发模式。

一个可检查、可扩展的事件驱动常驻 Agent Harness。自定义脚本工具使用 jsonschema 校验参数，图片处理使用 Pillow；MCP SDK 按需安装。

用户消息、工具回执、定时器和程序通知都进入同一事件队列，驱动 Session 执行 Step，
没有固定的 turn。Agent 通过 `send_user_message` 交付消息，发送后可以继续工作；
`wait_for_event` 让队列为空时等待新事件，后台发生器仍然运行。

## 环境与快速开始

- Python 3.11 或更新版本。Linux / WSL2 为完整运行环境；新增 Windows 原生可信开发预览见 [WINDOWS.md](WINDOWS.md)。其他 POSIX 系统未完整验证，Linux 定时器使用 `timerfd`。
- 默认启用的文件系统沙箱需要 bubblewrap 和 libseccomp，并要求宿主允许用户命名空间。Ubuntu/Debian 可安装 `bubblewrap libseccomp2`；不支持沙箱时会明确失败，不自动降级。
- 真实模型需要兼容 Responses API 的服务及自己的认证配置；也可选择 ChatGPT 登录，见下文。
- 核心回归测试和离线演示不需要模型凭据，不产生模型调用。

```bash
git clone https://github.com/WayneW1119/AsterHarness.git Aster
cd Aster
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
# Ubuntu/Debian：启用默认沙箱时需要安装这些系统依赖
sudo apt-get install bubblewrap libseccomp2
aster --help
aster --demo --workspace examples/workspace
```

不需要安装 Codex、连接开发者电脑或使用仓库外的兄弟项目。每位使用者自行配置模型、认证和工作目录，
运行数据默认写入自己的 Aster home，不随代码分发。默认沙箱的更多说明见下文。

构建安装所需的 setuptools 不属于运行时依赖。包内 Markdown 是实际加载的运行指令，
会随安装包一起分发。

## Python SDK

`from aster import Aster, ModelConfig` 提供创建、恢复和常驻运行接口。
支持配置文件/模板、Python 参数、异步消息流、宿主事件和收发回调，复用同一个核心运行时。
详见 [SDK 文档与接口说明](docs/sdk.md)；离线完整示例为
[examples/sdk/offline.py](examples/sdk/offline.py)，真实模型交互示例为
[examples/sdk/resident.py](examples/sdk/resident.py)。

## 管理 Agent 配置与 Thread

推荐使用 `aster agent init` 创建模板，再通过 `aster start --agent …` 创建独立 Thread。
新安装默认数据位于 `~/.aster/`，可通过 `ASTER_HOME` 覆盖；已有安装会兼容沿用 `~/.harness/`；每个托管 Thread 有自己的
`thread.toml` 和 `instructions.md`，支持创建时覆盖、独立编辑和显式 reload。
模板不持续覆盖已有 Thread，凭据通过环境变量或 `.env` 引用。
详见 [配置管理与完整命令](docs/design/agent-configuration.md)。

## 连接真实模型

```bash
cp .env.example .env
mkdir -p workspace
python3 -m aster --workspace ./workspace
```

编辑当前目录的 `.env`，设置（也可用 `--env-file /path/to/model.env` 指定）：

```dotenv
HARNESS_RESPONSES_URL=https://api.example.com/v1/responses
HARNESS_MODEL=your-model
OPENAI_API_KEY=your-api-key
```

地址须填写服务商提供的完整 Responses 接口路径，通常为 `/v1/responses`。已有进程环境变量优先于 `.env`。
可信内网 HTTP 接口需显式加 `--allow-http`。不要提交填写过的配置。

终端在 Agent 工作期间仍可接收新消息。`/quit` 正常保存并暂停 Thread：

```bash
python3 -m aster --list-threads
python3 -m aster --resume THREAD_ID
```

新 Thread 默认保存在 Aster 数据目录的 `threads/` 下。使用 `--thread-dir PATH` 可指定存储目录，
`--instructions-file PATH` 可为新 Thread 设置专属指令，
`--trace-dir PATH` 可额外保存详细记录。参数以 `--help` 为准。

### MCP 工具

可选安装 `python3 -m pip install '.[mcp]'`，新建 Thread 时通过
`--mcp-config PATH` 加载 `[mcp_servers.<name>]` TOML 配置，连接本机 stdio 或远程 Streamable HTTP 服务。
运行时发现工具，通过原生 `tool_search` 延迟提供定义；来源列表进入 WorldState，普通工具
回执仍走事件队列。支持列出资源、资源模板及读取资源。搜索定义属于普通历史，compact
覆盖后即移除，需要时重新搜索；resume 重新连接已保存的服务，不重放旧调用。
普通 API Key 接口需支持原生工具搜索及 namespace；不支持时不会自动切换成普通函数模拟。ChatGPT 登录适配器在传输层转换工具搜索格式，见 [ChatGPT 接入说明](docs/chatgpt-plan.md)。
详见 [MCP 工具发现与上下文](docs/design/mcp-discovery.md)。
示例配置在 [examples/mcp/config.toml](examples/mcp/config.toml)，完整边界见
[MCP 设计与使用](docs/design/mcp.md)。宿主可通过 Session.set_mcp_servers 在 Step 边界更新服务配置，触发 WorldState 来源增删，
并持久化供 resume 使用。支持 Bearer Token 环境变量和自定义请求头；尚无模型侧管理工具、服务端目录通知监听或 OAuth。

### Agent 自定义脚本工具

Agent 可用 `manage_tool_group` 管理分组，用 `manage_custom_tool` 注册、更新和移除自己编写的脚本。
来源摘要进入 WorldState，具体工具通过 `tool_search` 延迟加载，与 MCP 共用发现和上下文机制。
脚本以 JSON stdin 接收参数，在独立子进程中执行；注册时保存入口副本，下一 Step 生效。
支持参数校验、版本检查、超时清理、compact 与正常 resume，不需要 MCP 服务。
详见 [自定义工具设计](docs/design/custom-tools.md)。
普通宿主工具、MCP 和自定义工具共用 [namespace 注册与路由](docs/design/tool-namespaces.md)，
支持组说明、同名成员、直接提供及延迟加载。

## 核心机制

### Session、Step 与工具

每个 Step 消费当前排队的输入，捕获一致的 Prompt 与工具绑定，调用模型，再执行工具。
模型请求期间到达的输入留在队列中，后续 Step 继续处理。

- `send_user_message(text)` 默认产生普通工具回执，后续 Step 可继续工作。
- `wake_model=false` 成功时只保存配对回执；发送错误仍会唤醒模型。
- `wait_for_event()` 保存回执，但不额外发送消息或制造唤醒事件。
- 普通 assistant 文本不会自动当作用户可见回复。
- `exec_command`、`write_stdin` 支持后台进程、输出收集及命令完成通知。`list_processes` / `stop_process` 按当前 Session 的句柄查询和停止命令，非 TTY 也支持 Ctrl+C。详见 [进程管理](docs/design/process-management.md)。
- `apply_patch` 提供 Codex 风格的文件补丁。
- 定时器和 Agent 自己维护的 Python 程序可以成为事件源。
- `test_program_generator` 在临时状态目录自测程序，收集的事件不会进入正式队列。

发生器通知是数据，不代表新的用户授权。默认连续 Step 预算在
`SessionConfiguration` 中设置；应用嵌入方可以显式调整。

### 工具输出与图片

工具结果默认使用每条约 10000 token 的统一文本预算（UTF-8 字节 / 4 估算）。超额结果保留完整归档，
通过 `read_tool_output` 精确分页读取。原生工具 schema 与记忆版本证据保持完整。
支持 `view_image`、用户图片附件和 MCP 图片结果，图片以原生内容传给模型，并保存到 Thread 私有附件目录。
详见 [工具输出预算与图片](docs/design/tool-output-and-images.md)。

### 文件系统沙箱

新建 CLI Thread 默认在 Linux/WSL2 使用 bubblewrap＋seccomp：工作区可读写，未匹配规则的路径禁止访问；显式 `/=read` 可开放其余路径，额外路径可配置 read/write/deny，不涉及审批。`--deny-path`、`--read-only-path`、`--writable-root` 或 `--filesystem-policy` 设置例外。SDK 通过 `SessionConfiguration(filesystem_policy={...})` 开启，旧 Thread 按保存配置恢复。

单个 Thread 可用 `[sandbox] enabled = false`、`rules = []` 关闭沙箱，并通过 reload 或 resume 应用；仍以宿主普通用户运行。宿主可在运行中调用 `await session.set_filesystem_policy(...)`（传 `None` 关闭）；更新在 Step 边界生效，停止旧权限进程并更新 WorldState。详见 [沙箱机制、上下文与边界](docs/design/filesystem-sandbox.md)。

### Skills

默认扫描工作目录的 `skills/`，每个 skill 以 `SKILL.md` 的 YAML 元信息声明名称和简介。WorldState 在记忆附近呈现目录；完整指南与参考资料通过 `skills.read` 按 ID 读取，不依赖路径，也不受文件沙箱限制；脚本执行仍遵守普通工具权限。

```bash
aster --workspace ./workspace --skills-dir /path/to/shared-skills
```

`--skills-dir` 可重复指定，作为默认目录的补充。运行中目录变化在下一 Step 追加到历史；compact 后保留最新目录，使用指南时重新读取全文。可通过配置的 `skills.sources` 选择是否展示路径，运行时使用 `skills.load/unload` 管理；来源和卸载状态随 Thread 恢复。详见 [Skills 设计](docs/design/skills.md)，格式示例见 [review-text](examples/skills/review-text/SKILL.md)。

### Code Mode（可选）

安装 `python3 -m pip install '.[code-mode]'`，新建 Thread 时加 `--code-mode`。
额外提供 `functions.exec` / `functions.wait`，用 JavaScript 顺序或并行编排现有工具，
通过 text/image 选择输出，store/load 保存实例内临时数据。回执和完成通知仍走事件队列；
私有记忆修改和 compact 保持直接调用。详见 [Code Mode 设计与使用](docs/design/code-mode.md)。

### WorldState 与上下文

WorldState 各部分通过统一接口呈现固定基线、发布历史更新、验证状态并参与 compact。
当前包含记忆、skills、环境权限和工具来源，后者同时覆盖 MCP 与自定义工具。后续部分可通过宿主注册接入，
无须向请求组装和压缩流程添加各自的判断。详见 [WorldState 生命周期](docs/design/world-state-lifecycle.md)。

### 记忆、历史与压缩

每个 Thread 独立保存用户维护的 `instructions.md` 和 Agent 维护的 Markdown 记忆。
主记忆 `memory/index.md` 有独立的固定上下文基线，后续修改通过工具历史携带明确版本，
而不是每一步静默覆盖整个前缀。

Agent 使用 `read_memory`、`write_memory`、`apply_memory_patch` 管理记忆。
每次模型请求前验证可见记忆与磁盘是否一致；通过 shell 绕过记忆工具修改
`index.md` 会违反该不变量。记忆是可修订的工作数据，不是更高权限的指令。

默认记忆指令引导 Agent 用 Markdown 任务树记录职责、当前工作、计划、等待和暂放事项，
保留已验证进展、下一步和阻塞条件；持续职责与本轮工作分别标注。
这只是现有记忆的使用约定，没有新增计划工具、调度器或状态机。
详见 [通过记忆维护工作与计划](docs/design/memory-work-planning.md)。

历史分为完整归档与模型有效视图，通过稳定 ID 关联。支持：

- 范围压缩、嵌套压缩和跨恢复保留压缩结构。
- 固定快照编号、稳定路径、历史分页和长条目分段读取。
- 摘要直接来源与原始记录查询，以及远期归档关键词搜索。
- 压缩时保留精确记忆基线或检查点，不以自然语言摘要替代记忆版本。

默认在服务端报告的单次请求输入达到 100,000 tokens 时，向 Agent 发送
`context_budget_warning` 运行时事件；同一有效历史代次内，每再增长 25,000 tokens
才再次提醒。压缩后重新计量，正常恢复 Thread 会保留提醒去重状态。
统计包含缓存输入，不是累计账单用量；不包含刚生成的回复和随后新增的工具回执。
服务端未提供有效 `input_tokens` 时不猜测用量。提醒不是模型上下文上限，
不会强制压缩，由 Agent 自主选择范围和时机。

新建 Thread 时可使用 `--context-budget-warning-tokens 60000` 调整阈值，
`0` 关闭；再次提醒的间隔为阈值的四分之一（向下取整，至少 1 token）。
嵌入方通过 `SessionConfiguration.context_budget_warning_tokens` 设置正整数或
`None`（关闭）。配置随 Thread 保存；旧 Thread 缺少此字段时采用默认值。

设计文档：
[Codex 结构对照与语言路线评估](docs/design/codex-alignment-audit.md)、
[Python 运行时重构与行为契约](docs/design/python-runtime-refactor.md)、
[记忆投影](docs/design/memory-projection.md)、
[历史压缩](docs/design/compact.md)、
[历史导航](docs/design/history-navigation.md)、
[历史查询](docs/design/history-query.md)、
[上下文更新](docs/design/context-updates.md)。

### 定时与恢复

定时器持久化绝对的系统时间期限，通过
`timerfd(CLOCK_REALTIME, TFD_TIMER_ABSTIME | TFD_TIMER_CANCEL_ON_SET)`
等待内核通知，再接入 asyncio 文件描述符就绪事件；不使用长时间单调时钟 sleep 或轮询。
系统时间跳变时重新核对同一期限。Python 3.11/3.12 通过标准库 ctypes 调用 libc。

正常暂停后 resume 会恢复历史、记忆投影、待处理事件和发生器登记，不重新执行旧工具。
过期定时器补发一次，周期定时器不产生追赶式通知洪峰。活跃程序从入口重新启动，
保留其状态目录；旧命令进程句柄不能恢复。

宿主或虚拟机整体暂停期间，Agent 无法执行或交付消息。当前支持正常退出后的恢复，
不保证任意崩溃点的精确恢复。

### 请求兼容与日志

Responses 适配器将待完成工具批次的回执排在期间到达的新消息之前，以兼容要求先完成
调用/回执配对的服务。原始队列、归档和 rollout 保持到达顺序，不伪造回执或重放工具。

Trace 包含请求、响应、流事件、工具调用和结果；认证请求头不写入记录。
业务内容、文件文本和模型输入仍可能包含敏感信息，应自行保护日志。

```bash
python3 -m aster.compact_trace INPUT.jsonl --output OUTPUT.md
python3 -m aster.render_trace INPUT.jsonl --output EXPANDED.md
```

前者只展开一次固定指令和工具定义，保留完整动态输入输出；后者提供逐事件展开记录。
格式见 [运行记录设计](docs/design/compact-trace.md)。

## 开发与验证

完整核心回归不调用真实模型：

```bash
python3 -m unittest discover -s tests -v
```

专项示例：

```bash
python3 -m unittest discover -s tests -p test_timer_wait.py -v
python3 -m unittest discover -s tests -p test_runtime_resume.py -v
python3 -m unittest discover -s tests -p test_memory_projection.py -v
python3 -m unittest discover -s tests -p test_history_navigation.py -v
```

`scripts/` 中的验收入口使用合成夹具与独立 Thread；需要真实模型的脚本会读取本机
`.env` 并实际调用接口。仅在准备好配置和调用费用后显式运行：

```bash
python3 scripts/smoke_agent.py
python3 scripts/memory_patch_agent.py
python3 scripts/history_query_agent.py
python3 scripts/performance_agent.py
python3 scripts/compact_check.py
python3 scripts/navigation_check.py
```

输出写入忽略的 `test_runs/`，不作为可分发源码。实验结果不等于稳定成功率或并发性能指标。
CI 在 Linux 的 Python 3.11 与 3.13 上运行回归，并在仓库外检查安装后的 CLI、打包指令和离线 SDK；可选能力另行安装并测试。

## 目录

| 路径 | 内容 |
|---|---|
| `agent_harness/` | 运行时、工具、事件源、上下文、持久化、CLI 与日志渲染 |
| `tests/` | 可重复的核心回归测试及合成夹具 |
| `examples/` | 通用指令、工作区与程序发生器示例 |
| `scripts/` | 可复用开发验收入口 |
| `docs/design/` | 功能设计与边界说明 |
| `third_party/` | 第三方适配归属、许可证和 NOTICE |

本仓库不包含开发者的 Thread、记忆、凭据、运行记录、手机测试网站、网关配置或个人部署脚本。
本地保留的这些文件已列入忽略规则，不是启动项目的前提。

开启沙箱时，本地命令和程序发生器遵守配置的 Linux 隔离边界；关闭沙箱后按宿主用户权限执行。
当前不提供网络隔离，其他边界详见沙箱文档。第三方补丁适配归属见
[third_party/README.md](third_party/README.md)。

ChatGPT 套餐接入使用官方 OAuth，模型和额度以自己的账户实际授权为准。先安装 `python -m pip install '.[chatgpt]'`，再运行 `aster auth login --no-browser`，再用 `aster auth models` 和 `aster auth probe` 验证。Agent 配置可设置 `model.auth = "chatgpt"`；详见 [ChatGPT 套餐接入](docs/chatgpt-plan.md)。

Linux/WSL sandbox profiles can opt into local Unix socket IPC with `[sandbox] local_ipc = true`. This retains filesystem restrictions and provides private per-execution temporary directories by default. Visible host services may act beyond file grants; see the bundled filesystem sandbox reference for exact boundaries.
