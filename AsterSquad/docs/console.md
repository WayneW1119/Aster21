# 多 Squad 工作台与 Relay 域切换

## 使用

```bash
aster-console --root ./demo-data --mode fake
```

工作台默认在 `http://127.0.0.1:19910`，使用原有 `control/owner.token` 登录。已有默认 Squad 的账号、消息和 Thread 不重建；它由工作台作为独立子进程运行，内部 HTTP 使用 19912，MCP 与默认 Relay 保留原端口 19911、19920、19921。

左侧 **＋** 创建 Squad，填写名称、说明和模式。每个 Squad 有独立数据、成员、默认 Relay 域。新实例自动分配并保存端口；暂停的实例所占端口不会分给新实例。空间目录按需分配，不设固定的 8 个空间上限；每个 Squad 仍最多 16 位成员。

工作台保留单实例运行方式：`python -m squad` / `aster-squad`；多实例入口为 `run.py`、`python -m squad.fleet` 或安装后的 `aster-console`。

## 分支与成员管理

- 分支图展示独立 root 与其 child；侧栏切换 Squad 不混合成员。
- 点击节点查看职责、模型、状态、Relay 用户名、直接下级和实际 instruction。
- 顶部新增成员可以选择 parent；节点下方 **＋** 直接在对应分支创建 child。
- 编辑模型、职责与可视化路径授权；自己的工作区不需要重复填写。
- 单独起停、修复聊天连接、查看基础终端；批量启动和正常暂停。
- 分支图与表格切换、按成员或职责搜索、缩放。
- 归档保留 Thread、工作区、消息与身份，占用原来的成员位置；同时停用对应 Relay 账号。需先处理活跃 child 才能归档 parent。
- 恢复归档成员时保留原 Thread，先恢复 parent，再恢复 child；恢复后按需启动。

设置中可以重命名 Squad、修改说明和正常暂停整个空间。暂停空间会正常关闭成员与默认聊天域。工作台重启时会重新启动此前启用的服务，成员是否恢复由空间的「空间启动后恢复成员」决定；此前手动暂停的空间保持暂停。

## Relay 的多个域

工作台的「Relay 聊天域」视图列出各 Squad 的默认域，也可以创建不属于任何 Squad 的独立域。

点击「打开 Relay」会将当前用户可用的域连接交给 Relay 网页，使用 URL fragment 传递，页面读取后立即从地址栏清除，不进入 HTTP 请求日志。这里只传用户自己的域账号凭据，不传工作台管理凭据。

Relay 左侧有域快捷栏，域名称按钮打开完整选择器。每个连接分别保存服务 URL、账号凭据、域名称和用户名。切换时取消旧请求、清空会话和草稿视图，再认证新域；旧请求不能覆盖新域的状态。

选择器还可以手动添加已有服务地址和该域分配的凭据，因此 Relay 单独运行时也能切换多个域，不依赖 Squad。移除连接仅影响浏览器，不删除服务、账号或消息。传统 token 连接保存在当前标签会话的 sessionStorage 中；密码/通行密钥登录由该域独立的 HttpOnly session Cookie 保持。持久的域提示不包含机器凭据。

本机不同端口通过明确的 localhost/127.0.0.1 CORS 来源规则访问；每次请求仍必须提供目标域的有效凭据。用户名相同不代表是同一个账号，源域凭据不能登录另一个域。账号认证失败会要求重新登录，不会复用当前域的凭据尝试其他域。

## 管理入口 API

均需要工作台 owner Bearer token，不向 agent 暴露这些宿主级管理能力。

| 接口 | 用途 |
|---|---|
| GET /api/state | 空间名册与默认 Squad 快照 |
| POST /api/squads | 创建 Squad：name、description、mode |
| POST /api/domains | 创建独立 Relay 域 |
| POST /api/resources/{id}/settings | 重命名和修改说明 |
| POST /api/resources/{id}/start 或 stop | 起停整个空间 |
| GET /api/squads/{id}/state | 指定 Squad 详情 |
| POST /api/squads/{id}/agents | 新增成员，可指定 parent |
| POST /api/squads/{id}/agents/{agent}/configure | 配置成员 |
| POST /api/squads/{id}/agents/{agent}/archive 或 restore | 归档与恢复 |
| POST /api/squads/{id}/bulk | 批量 start/stop，返回成功和失败列表 |
| GET /api/relay-connections?selected={id} | 当前用户的 Relay 登录连接集合 |

原默认 Squad 的 `/api/agents/...` 路径仍代理到默认实例，保留本地脚本兼容性。单 Squad HTTP API 仍可独立使用。

## 隔离与状态

工作台持久化注册表在 `control/fleet/console.db`。额外 Squad 使用按需编号的 `instances/slot-XX`；独立 Relay 域保存在控制器私有目录。

真实模式且启用沙箱时，除了屏蔽同 Squad 的其他工作区，还强制屏蔽其他 Squad 的数据目录、默认实例的控制器与运行数据。路径授权不能重新打开这些目录。

真实模式的 Aster home 使用独立的 `runtime/slot-*`，自己的运行文件保持默认只读，其他 slot 被屏蔽。Aster 原生发生器通过受控的合成路径挂载自己的状态；它的状态源目录不放在显式 deny 的控制器目录下面。已有演示 Thread 的 home 路径保持不变。

用户可在创建或编辑成员时切换沙箱，默认开启。关闭后以宿主用户权限执行，保存的路径规则不生效但不删除；再次开启后恢复。仅改路径或沙箱开关时，在执行步骤边界在线应用，worker 与 Thread 保持；等待期间显示待生效。修改模型或身份时正常暂停并恢复原 Thread。宿主 systemd 授权是另一个独立开关，开关沙箱不会改变它。

## 验证

- `test_fleet.py`：实际进程创建两个 Squad、跨实例同名成员、parent 分支、归档恢复、独立域、跨域认证拒绝、CORS、重命名、暂停与工作台重启持久性。
- `test_sandbox.py`：真实 bubblewrap 下保护控制器和 peer 数据；真实 Squad 规则下发生器可写自己的状态并成功唤醒。
- `browser_console.py`：从网页创建 Squad、沿分支创建 child、编辑、起停、归档恢复同一个 Thread、创建独立域、域导入/切换/移除/重连、消息隔离与手机布局。
- `browser_smoke.py`：网页私聊、已读、账号创建/重置/停用/启用、HTML 文本转义。

这些验收均不调用付费模型，也未修改 Aster 仓库。

## 在同一个 Squad 使用不同模型服务

工作台和独立 Squad 均接受 `--model-catalog /absolute/path/models.json`（也可用 `SQUAD_MODEL_CATALOG`）。目录由宿主管理，键是界面和 MCP 可选择的模型 ID；每个条目独立指定模型、接口和认证：

```json
{
  "deepseek-flash": {
    "name": "deepseek-flash",
    "url": "https://api.deepseek.com/responses",
    "auth": "api_key",
    "api_key_env": "SQUAD_DEEPSEEK_API_KEY"
  },
  "gpt-6.1-sol": {
    "name": "gpt-6.1-sol",
    "url": "https://api.openai.com/v1/responses",
    "auth": "chatgpt",
    "auth_home": "/home/you/.harness",
    "auth_profile": "default"
  }
}
```

先通过 Aster 的 `aster auth login` 登录 ChatGPT。API Key 只放宿主环境或受保护的 EnvironmentFile，模型目录不接受明文密钥。API 模型 worker 只接收所选服务的密钥；ChatGPT worker 不接收 API Key，也不回退到 API 计费。所有配置过的 ChatGPT credentials 目录都被列入成员沙箱 deny。界面与 MCP 只发布可选 ID，agent 不能指定任意 URL、凭据路径或环境变量。

新建真实 Squad 后，成员的模型下拉框会同时列出这些 ID；新增 child 同样可独立选模型。修改模型继续使用原 Thread，宿主会完整替换认证配置，避免切回 API 模型后残留 OAuth 设置。旧的 `--models`/`--responses-url` 单服务配置仍兼容；演示目录不会被转换成真实 Thread。


## 额外 MCP 服务

成员创建与编辑页在宿主配置额外 MCP 服务后显示“MCP 服务”选择项，详情显示已配置名称。宿主用 `--mcp-catalog PATH` 或 `SQUAD_MCP_CATALOG` 提供目录，格式见 [示例](../examples/mcp-services.json)。支持 Harness 标准 stdio/HTTP 配置、环境凭据引用与工具限制。工作台只发送服务 ID。`default=true` 对未单独配置的成员生效，显式空列表表示停用所有额外服务。

运行中的空间会识别目录里新增的 `default=false` 条目。支持热更新的 worker 在下一个 Step 边界应用单独的 MCP 选择变更。新增服务若引用环境变量，或同时修改了其他需要重启的配置，成员会正常暂停并恢复原 Thread，以便按宿主配置注入凭据。目录里已有定义的修改、删除和默认值调整在重启空间时生效。自定义 Thread 连接会保留，内置 Squad 和 Relay 连接由宿主管理。

在线应用以回执为准：pending 表示等待步骤边界；必需服务失败时保留原配置；可选服务失败时保存配置、允许其他服务继续运行，并在工作台列出 `unavailable_mcp`，不显示成全部连接成功。修复服务后取消勾选并保存，再重新启用可触发一次重连，不会自动无限请求。

浏览器的可选 MCP 与 skill 示例见 [Playwright browser](../examples/browser/README.md)。该示例默认关闭，采用独立临时会话和空闲释放；实际可用性取决于浏览器与宿主沙箱的兼容性，读取 skill 不授予浏览器权限。

网页检索是独立的 [Aster Web](https://github.com/WayneW1119/AsterWeb) MCP，可同时使用 Exa、DeepSeek。安装和私有运行配置由部署者管理，Squad 本身不依赖该包。配套 skill 可复制至宿主共享 skill 目录，不放进成员 workspace。

图片生成与编辑可接独立 [Aster Images](https://github.com/WayneW1119/AsterImages)：宿主运行持有 Codex ChatGPT 登录的私有 worker，成员通过标准 stdio MCP 读取授权图片、提交字节并保存结果。提供 `image_generate` / `image_edit`，结果为文件与标准 MCP 图片块；Harness 可将其送入支持图片输入的模型。连接目录只引用环境变量，工作台不展示 token。示例 images 条目默认关闭，安装服务后可由部署者调整默认值或按成员启用。图片 skill 应注册在共享目录；不预置到私人 workspace。

## 路径权限编辑

成员编辑页将路径分成「只读」「可读写」「禁止访问」三组，每组分别添加、编辑和删除；手机上的长路径可以换行编辑，不需要填写 JSON 或切换小型下拉框。工作区默认可写，未匹配规则的路径禁止访问；原全局只读表现为普通 `/=read` 路径行，可以编辑或删除；系统保护的禁止访问路径单独列出，不能删除。离线演示模式会明确说明没有操作系统级文件隔离。

可读写组可填写尚不存在的绝对路径，保存时按目录创建（包括缺失的中间目录），只授权指定目标。它不是创建新文件的入口；新文件应在已授权目录中由 agent 创建。只读和禁止访问组要求路径存在，所有规则禁止经过符号链接及使用 `..`。新目录需要宿主进程拥有创建权限。删除路径规则并保存只撤销规则，保留目录和文件。

更具体的路径规则优先，同一路径 deny > write > read；可在禁止父目录内开放指定子目录。不能禁止自己的工作区本身，系统默认的可写 workspace 是父目录隔离中的预设例外。下级不得借较大的授权目录绕过上级对子目录的只读或禁止访问限制；收窄上级规则前须先收窄仍持有相应授权的下级。

## Relay 群聊

工作台的 Relay 同源代理支持群聊列表、管理和消息接口，权限由 Relay 自己验证，Squad 登录凭据不会作为群聊权限。需要 Relay 0.3 或更新版本；群聊本身独立于 Squad，可直接在 Relay 网页或 MCP 中使用。

## 成员运行指标

分支卡片和成员列表显示实际活动状态、最近请求输入 token 和该次缓存命中率；详情另列最近输出、测量时间和最近最多 20 次已完成请求的加权缓存命中率。活动状态来自 Harness 公开 on_trace 事件，生命周期仍由进程管理状态决定，暂停/错误不会显示成正在工作。当前工具只显示名称，不上传参数、结果或模型内容。

输入 token 由服务端上报，包含指令、记忆、历史和工具定义，缓存部分仍占上下文；它不是实时上下文估算，也不是模型最大窗口。压缩后标记等待下一次测量。缓存字段缺失显示“未上报”，不当成零；加权命中率只使用有缓存字段的请求，旁边列出覆盖次数。窗口与最近测量保存于 Squad 数据库，跨 worker 重启保留；切换模型后新测量开始独立窗口。演示模型没有真实用量。

正常暂停等待当前步骤写入检查点。界面等待超时只返回“仍在安全暂停”，成员保持 stopping，不杀 worker、不改配置，也不允许重复启动同一成员。整空间暂停同理；控制器正常关闭等待全部成员，systemd 模板不设置自动强杀截止时间。网络断流等未提交模型请求由 Harness 退避重试，工作台显示“重试中”；永久额度/认证问题仍显示明确错误。异常恢复通过公开 Aster.recover 执行并审计，不能手工伪造正常退出或重放工具。

普通路径权限采用更具体者优先，同路径 deny > write > read；父目录 deny 下可以重新开放指定子目录。平台固定保护与普通规则分开：默认整体隐藏数据区，仅开放成员自身所需目录，不再按每个预留成员展开三组禁止路径。工作台展示默认隔离区和预设例外，用户编辑列表仅保存自定义授权。父子转授与收窄按整套有效权限检查，不能通过宽目录遗漏下级限制。parent 修改直接 child 时只需有权调整实际变化区域，范围外的已有权限须保留；用户独立给 child 的其他项目授权不阻塞这次保存。收窄 parent 只检查本次实际收窄区域是否仍被 child 使用。

## 以宿主 root 身份运行

工作台创建或编辑成员时可设置 `run_as_root`，默认 false。只有用户可以授予或撤销，parent 的 MCP 不能转授，child 不继承。它改变整个 worker 的 Linux 身份，原有文件、命令、MCP 和事件工具继续使用；没有新增 root 命令工具或逐命令审计功能。切换需要正常暂停并恢复同一 Thread，暂停失败时原配置保留。

root 身份期间暂时不应用路径沙箱，但保留 `sandbox_enabled`、grants、模型、身份、账号与记忆；关闭后恢复保存的沙箱配置。root 能管理整台 Linux/WSL，不包括 Windows 管理员。项目和业务目录内由 root 创建的文件遵循 Linux 属主规则，不自动改回普通用户；仅成员内部 runtime 数据在启动准备和退出时交还宿主用户，以支持后续普通身份恢复。

宿主管理员做一次安装（替换 Python 路径为实际 Squad 环境；控制器继续以普通用户运行）：

```bash
sudo /usr/bin/python3 deploy/install_root_worker.py \
  --python /absolute/path/to/venv/bin/python
```

默认授权数据根为该用户的 `~/.aster-squad`；自定义部署增加 `--data-root /absolute/data/root`。启动器安装于 `/usr/local/libexec/aster-squad-root-worker`，管理员配置位于 `/etc/aster-squad/root-worker.json`，sudoers 只允许这一固定启动器，不开放通用 shell 或 Python 命令。未安装或检查失败时开关不可启用，启动失败不降级成普通身份。更新启动器代码后管理员需重跑安装命令；不因此授予任何成员 root。

启动器核对成员身份、真实空间、数据库中的 root 授权、固定 runtime/workspace 与宿主数据范围。模型和 MCP 的必要环境由私有启动管道传递，不增加凭据文件。它运行的是宿主已安装的 Squad Python 环境，因此宿主账号和该环境必须可信；当前多成员共用 Linux 用户，root 开关不构成针对同 UID、已脱离沙箱进程的账户隔离。撤销会正常结束当前 worker 后以普通身份恢复，不撤销已完成的系统改动或另行托管的服务。

## 删除空间

空间设置中的「删除空间」会先正常停止成员与默认聊天域，再从工作台移除登记，不擦除本机成员、聊天、记忆或项目文件。若安全暂停超时，登记和数据都保留，稍后可以重试，不强制结束进程。已移除登记的副本在 `control/fleet/removed/<id>.json`；数据留在原目录，名称可以重新使用，新空间会分配全新目录，不接管或覆盖旧 Thread。删除最后一个空间后重启工作台也不会自动重建旧的默认空间。物理删除或恢复旧目录是独立的宿主管理操作。

空间隔离通过共享数据根的默认隐藏及本成员访问例外实现，新增空间也处于既有隐藏范围内；路径授权不能重新开放其他空间或未来空间的实例目录。

成员配置的「允许使用 GPU」独立于路径权限，仅用户可开启，当前支持 WSL2 `/dev/dxg`。默认关闭且不自动传给下级；保存后在步骤边界更新，结束旧受管命令并恢复监听，保持原 Thread。关闭沙箱或 root 身份期间保留设置、按宿主设备权限运行。

GPU、用户级 systemd、关闭沙箱及额外 MCP 服务支持在直接 parent 已有显式权限范围内转授；root 仍仅用户可设置。新成员不自动获得 GPU/systemd 或关闭沙箱。收窄上级时，若下级仍持有对应授权，须先自下而上收回；不会自动删除服务或修改范围外的用户独立授权。agent 使用 squad_create/squad_configure 的 gpu_access、host_systemd、sandbox_enabled、mcp_services 参数管理下级；权限待生效期间禁止转授。

启动分两层：工作台设置控制宿主启动器是否随系统启动；空间设置 `restore_members` 控制该空间启动后是否恢复此前请求运行的成员，默认关闭。保存空间设置不会立即启动或暂停成员。显式启动记录运行意图，主动暂停或归档清除它；宿主安全停机保留它。空间启动且 MCP/Relay 就绪后，最多同时恢复两位，每次生命周期只尝试一次；已报错、主动暂停和归档成员跳过。未正常关闭的 Thread 不绕过恢复校验，Windows 管理员仍遵循 UAC。已暂停的空间不会因恢复设置自动开启。旧版成员级 `auto_start` 仅在尚未明确保存空间恢复策略时兼容；空间设置一旦保存即统一覆盖旧标记。

工作台「工作台设置」中的整体开关操作真实宿主启动配置，不是数据库中的占位标记。Linux/WSL 使用已安装的用户级 systemd 服务，通过 `--startup-unit NAME.service` 或 `ASTER_SQUAD_STARTUP_UNIT` 指定，仅执行该服务的 enable/disable，不停止当前工作台，不修改 VPN、Windows 转发或其他服务。没有注册启动器时显示不可用；部署模板包含注册配置。WSL 需要 Windows 先启动发行版；开关不负责启动 Windows 或 WSL 本身。

Windows 桌面启动器注册当前用户 HKCU Run 项，在登录后启动现有 desktop 配置及其启用的配套服务；不申请管理员权限。自启动仍可能遇到 UAC 或凭据失效，失败由用户处理。直接运行 Fleet 而非桌面启动器时不自动创建 Windows 启动项。

`GET /api/startup` 查询启动器支持状态和实际开关，`POST /api/startup {"enabled":true|false}` 修改。两者均需要工作台用户凭据；空间设置沿用 `POST /api/resources/{id}/settings`，支持布尔值 `restore_members`，暂停中的空间也可保存，不会因此启动。独立 `aster-squad` 使用 `POST /api/settings` 设置相同字段。

桌面启动器使用 ChatGPT 登录时提供 `gpt-6.1-sol`、`gpt-6.2-astra` 和 `gpt-6.2-sol`。已有成员不会自动换模型。自定义模型目录可按同样格式添加新模型，各条目可共用认证配置；更新目录后重启工作台加载。
