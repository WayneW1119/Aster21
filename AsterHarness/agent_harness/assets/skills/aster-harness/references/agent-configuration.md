# Agent 模板与 Thread 配置

新安装默认数据根目录为 `~/.aster/`，可用 `ASTER_HOME` 指定另一套数据。
若 `~/.aster/` 不存在且已有 `~/.harness/`，会继续使用旧目录，不自动移动正在使用的数据。
`HARNESS_HOME` 仍兼容，优先级低于 `ASTER_HOME`。
它不依赖源码所在目录。Python SDK 的 `ThreadManager(root)` 仍使用显式传入的目录。

```text
~/.aster/
├── agents/<模板名>/
│   ├── agent.toml
│   └── instructions.md
├── credentials/default.env
└── threads/<UUID>/
    ├── thread.toml
    ├── instructions.md
    ├── rollout.jsonl
    ├── memory/index.md
    ├── traces/run-<session-id>.jsonl
    ├── runtime.json             # 正在运行的托管 CLI 标识；退出时移除
    ├── reload.request.json      # 宿主控制请求，不是模型工具
    ├── reload.status.json
    └── …                       # 附件、工具、发生器等原有私有数据
```

模板只是创建时的起点，不是持续继承的父配置。创建 Thread 时复制完整有效配置和
instruction；之后改模板不会影响已创建的 Thread。模型连接凭据仅引用环境变量和
凭据文件，不把 API Key 写入 TOML、rollout 或请求日志。外部 MCP 配置、Skills
目录和凭据文件是显式的外部引用，不复制其内容；修改后要恢复或 reload 才重新加载配置。

## 创建与使用

安装后主命令为 `aster`，兼容命令 `harness` 和 `aster-harness` 指向同一个入口。也可以始终使用
`python -m aster` 代替 `aster`。Python 需要 3.11 及以上。

```bash
mkdir -p ~/agent-workspaces/assistant
aster agent init assistant --workspace ~/agent-workspaces/assistant
aster config edit assistant --agent
aster instructions edit assistant --agent
```

编辑命令使用 `$VISUAL` 或 `$EDITOR`，未设置时使用 `nano`。
配置里的模型名称起初为空，需要填写。默认引用
`~/.aster/credentials/default.env` 中的 `OPENAI_API_KEY`；凭据文件由用户填写：

```dotenv
OPENAI_API_KEY=your-key
```

也可以在 `model.env_file` 引用现有 `.env` 文件。`model.api_key_env` 可改成其他
变量名。已有进程环境变量优先于凭据文件；凭据文件仅提供凭据，不覆盖 TOML 中的模型或 URL。
修改凭据后需要恢复或 reload，现有模型客户端不会自动换 Key。

```bash
# 从模板创建一个有名称的独立 Thread，同时覆盖部分设置
aster start --agent assistant --name daily \
  --set 'model.name="your-model"' \
  --set 'context.warning_tokens=80000'

# 还可以给这个 Thread 选择不同工作目录
aster start --agent assistant --name another --workspace ~/agent-workspaces/another

aster threads
aster status daily
aster resume daily
```

工作目录必须事先存在。不传 `--name` 时自动生成名称；可以通过名称或 UUID 定位 Thread。
不传 `--agent` 时使用或创建 `default` 模板。第一次生成的默认模板同样需要配置模型。
托管启动默认不限制连续 Step；输入 `/quit` 正常暂停并保存。`--prompt` 支持批处理运行。
每次托管运行都在 Thread 的 `traces/` 中记录完整模型请求、回复、工具事件等；
rollout 是独立的持久历史。日志可能包含业务文件内容，但不保存 HTTP 认证头。

复制模板并修改，不影响原模板：

```bash
aster agent init researcher --from assistant \
  --set 'context.warning_tokens=90000'
aster agent list
```

`--agent` 和 `--from` 也接受配置文件或包含 `agent.toml` 的目录路径。

## 完整配置字段

```toml
name = "daily"
workspace = "/home/you/agent-workspaces/assistant"
instructions = "instructions.md"

[model]
name = "your-model"
url = "https://your-service.example/v1/responses"
api_key_env = "OPENAI_API_KEY"
env_file = "/home/you/.aster/credentials/default.env"
allow_http = false

[runtime]
max_consecutive_steps = "unlimited" # 或正整数

[context]
warning_tokens = 100000            # 0 关闭提醒；不是硬性上下文上限
# 单条工具输出的文本预算；可设 "unlimited"
tool_output_tokens = 10000

[sandbox]
enabled = true
rules = [
  { path = "/home/you/shared", access = "read" },
  { path = "/home/you/private", access = "deny" },
]

[tools]
code_mode = false

[skills]
directories = []
# sources 默认不展示文件路径；专用 skill 读取不受文件沙箱限制
sources = [{ path = "/shared/skills", expose_path = false }]

# 可选；与现有 MCP TOML 格式相同
# [mcp]
# config = "mcp.toml"
```

MCP 默认保持连接。仅对于不依赖进程内会话状态的 stdio 工具，可在该服务的
`[mcp_servers.<name>]` 中设置 `connection_mode = "on_demand"`：发现目录后退出，
每次操作临时启动并核对目录，结束后退出，保留沙箱且不重放失败调用。
这会增加调用启动开销，不适合事件监听、有状态工具或 HTTP 服务。

所有相对路径相对于配置文件所在目录，`~` 会展开。路径权限仍为工作区可写、
未匹配路径禁止访问，显式规则使用 read/write/deny。权限路径必须存在，不能经过符号链接。
若需要开启沙箱但让所有普通路径可写，可以设置 `rules = [{ path = "/", access = "write" }]`；内部保护和进程隔离仍生效。
创建 Thread 时把外部路径转成绝对路径，instruction 独立复制到 Thread 中。
已创建 Thread 的工作目录和 instruction 文件位置不能修改。原先未启用沙箱的
Thread 可以通过恢复、显式 reload 或宿主 Agent.set_filesystem_policy 更新；传 None 关闭沙箱，下一 Step 边界应用。profile 宿主同步保存 sandbox 配置供下次恢复。
权限规则可以修改。模板可以启用或关闭沙箱，以创建不同类型的新 Thread。

启用沙箱时，普通工具无法访问整个 Thread 存储、当前 ASTER_HOME 的
`credentials/`、`agents/` 及该 Thread 引用的凭据文件。专用记忆工具仍正常工作。
关闭沙箱时不提供这些 OS 级限制。

## 修改单个 Thread

```bash
aster config show daily
aster config edit daily
aster instructions edit daily
aster config check daily

# 不打开编辑器，直接修改；右侧是 TOML 值，字符串需带双引号
aster config set daily 'model.name="another-model"' 'context.warning_tokens=70000'
aster config set daily 'sandbox.rules=[{path="/home/you/private",access="deny"}]'

# 修改后明确应用到正在运行的 Thread
aster reload daily
aster status daily

# 未运行时，下次恢复采用配置文件；也可以在恢复命令中覆盖并保存
aster resume daily --set 'context.warning_tokens=90000'
```

`config set` 先校验再原子写入；规则数组是完整替换。未知字段和错误类型会报错。
`config edit`/`instructions edit` 是普通编辑器，可暂存未完成的内容，需要用
`config check` 检查；它们不会立即应用到正在运行的 Agent。

### Reload 的第一版语义

`reload` 向正在运行的托管 CLI 写入一个宿主控制请求；它不调用模型，也不让模型
自行修改权限。运行实例重新校验配置、凭据和沙箱后，完成当前 Step，正常暂停，
再恢复**同一个 Thread**。历史、记忆、待处理事件保留，已完成工具不重放。
配置无效则拒绝请求，原实例继续运行。`status` 查看 applying/applied/rejected。

这是一次受控的运行时重建：旧命令句柄和 Code Mode 临时数据不保留，活跃程序
发生器遵循已有 resume 语义从入口重启并保留状态目录。调用方需自行处理程序的幂等性。
修改权限经过既有的版本化 WorldState 转换；MCP 和 Skills 更新仍走各自的上下文机制。
模型连接和上下文提醒等宿主设置在恢复后生效，instruction 在托管实例内固定到显式 reload。
不存在自动文件热加载。服务连接或模型网络调用仍可能在应用后的运行阶段失败；
配置校验不等于远程服务可用性检查，不提供跨外部服务的事务回滚。

## 旧入口与数据

旧的 `--workspace`、`--resume`、`--list-threads` 等入口继续可用；其默认 Thread 根目录
也改为 `~/.aster/threads/`。旧入口仍使用原来的参数和 `.env` 规则，连续 Step 默认 12。
只有新的托管入口创建和使用 `thread.toml`。使用新入口创建的 Thread，推荐始终通过
`aster resume` 恢复，以采用完整配置并支持 reload。

不会自动移动原项目中的 `.harness/`。旧 Thread 可继续这样使用：

```bash
python -m aster --thread-dir /path/to/project/.harness/threads --list-threads
python -m aster --thread-dir /path/to/project/.harness/threads --resume THREAD_ID
```

这轮没有旧 Thread 自动导入或迁移命令。恢复仍要求上次正常关闭，不提供任意崩溃点的恢复。
源码、配置与工作区可以位于不同位置；凭据、Thread 和 traces 不应提交到 Git。


## 外部通信适配器

手机或其他通信适配器可以沿用已有 Thread 的存储位置，而直接读取该 Thread 的
`thread.toml` 和 `instructions.md`，不必重新创建 Thread。适配器使用
`load_profile`、`resume_profile` 和异步 `prepare_session` 接入同样的校验、配置快照、
权限版本迁移与 instruction 固定机制，并保留自己的收发消息和 trace 回调。
修改文件后由适配器正常暂停并恢复；只有托管 CLI 本身处理 `aster reload` 控制请求。
外部适配器需要提供自己的重启或 reload 入口。

可以在 `~/.aster/instances/<名称>` 建立指向该 Thread 目录的宿主软链接，
作为易于找到的配置入口；这不移动历史，也不改变工作区和 Thread 身份。
模板在 `agents/`，实例入口在 `instances/`，两者没有持续继承关系。

文件规则采用统一的未匹配禁止访问语义。`[sandbox].format = 2` 是存储格式标记，不是权限模式；新配置无需选择 default_access。旧版未标记配置和历史策略只在加载时转换为显式 `/=read`，不改写 rollout。保存后标记格式，删除根规则不会在下次恢复时被补回。

启用沙箱时可设置 `[sandbox] gpu = true`，单独开放受支持的 GPU 设备；默认关闭。GPU-only 变更也参与同一 Thread 的在线策略更新与恢复核对；关闭沙箱时不设置此字段。

Linux / WSL 的 `[sandbox] local_ipc = true` 独立开启本机 Unix socket 通信与默认隔离临时目录，默认关闭，仅启用沙箱时设置。边界及临时目录生命周期见 [filesystem-sandbox.md](filesystem-sandbox.md#本机进程通信linux--wsl)。

模型协议用 `[model]` 下的 `protocol = "responses"` 指定，省略时也使用 Responses。
`auth` 单独决定使用 API key 还是 ChatGPT 登录。Python SDK 对应参数是
`ModelConfig(..., protocol="responses")`。

选择 Anthropic Messages 时设 `protocol = "messages"`、`auth = "api_key"`，
并显式设置完整的 `/v1/messages` URL、对应供应商的模型名和 API 密钥环境变量名。

Agent 可以调用 `agent_time()` 查询当前时间与自己的固定 UTC 偏移。
调用 `agent_time(utc_offset="+8")` 设置偏移；持久 Thread 在恢复后沿用这个设置。
`create_timer(message="提醒", at="2027-01-05 09:00")` 按该默认偏移解释未来时间；
带偏移的时间按输入偏移解释。回执及生成事件的时间使用当前偏移显示。
修改偏移不会移动已有定时器的截止时刻。间隔仍用毫秒；固定偏移不自动跟随夏令时。
