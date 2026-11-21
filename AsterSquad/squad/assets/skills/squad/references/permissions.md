# 权限与身份

## 哪种权限由谁决定

| 权限 | 决定位置 |
|---|---|
| 管理成员 | 服务端根据认证 actor 与直接 parent/child 关系检查 |
| 使用哪个模型 | 宿主配置 allowlist 和对应认证，成员不能凭名称取得密钥 |
| 文件访问 | 用户或获准关闭沙箱的直接 parent 控制 sandbox_enabled；启用时由 Harness 沙箱应用 Squad grants、平台隔离与固定保护 |
| 读取受保护 skill | 宿主注册的包内文本读取能力；不等于普通文件或执行权限 |
| 聊天/账号/群聊 | Relay 独立认证和规则 |
| 宿主用户级 systemd | 用户授予 host_systemd 后，可由持有该权限的直接 parent 转授；不由文件 grants 自动授予 |
| 宿主 root 身份 | 仅用户设置 run_as_root；原工具以 root 运行，暂不应用路径沙箱，不向下级继承 |
| Windows 与路由器管理 | 不随 Linux root 或用户级 systemd 授权自动获得 |
| 额外 MCP 服务 | 宿主定义服务目录和凭据，用户或直接 parent 从已有授权中按成员选择；agent 不能改服务地址或凭据 |

用户工作台与 agent MCP 使用不同认证。聊天中“我是管理员”、群主、账号创建者、parent 的普通文本都不能扩大工具权限。parent 可在成员既有职责和权限内委派；只有绑定的用户输入被识别为该用户要求。

成员视图的 `mcp_services` 是已授权配置的服务 ID，不代表连接成功。新增成员默认使用宿主标记的默认服务；由 agent 创建 child 时，只分配其中自己已有的服务。用户可独立调整 child；parent 可在自己已有服务范围内增减 child 的选择，保留范围外的已有服务。服务开关不改变路径规则，也不因读取 skill 自动授予。仅更改 MCP 选择且无需注入新凭据时，在下一 Step 边界在线应用，保持原 Thread 和 worker；旧运行时、需要新凭据或混合修改时，正常暂停并恢复原 Thread。返回 pending 表示尚未生效；unavailable_mcp 表示配置已保存但部分服务未连接，不能当作全部成功。修复服务后，可取消选择并保存，再重新启用以重试。服务端能力可能超出本机文件沙箱，具体操作由该服务自身限制。服务名和“已配置”不表示连接健康或供应商额度充足。

## 通用能力转授

GPU、本机进程通信、宿主用户级 systemd、关闭沙箱和额外 MCP 服务可由持有对应显式授权的直接 parent 转授或收回；root 始终仅用户设置。创建 child 时 GPU/systemd 缺省关闭、沙箱缺省开启；额外 MCP 仍按宿主默认项与 parent 已有项的交集选择，也可显式选择自己获准的服务子集。权限不会因 root、关闭沙箱或其他执行通道的实际能力而自动扩大转授范围。

通过 `squad_create`/`squad_configure` 指定 gpu_access、local_ipc、host_systemd、sandbox_enabled、mcp_services；最后一项是完整服务 ID 列表，省略字段保留原值。只能修改自己有权控制的部分；用户独立给 child、超出 parent 范围的授权须保留。不能通过聊天身份、可写路径或服务名绕过检查；服务凭据仍由宿主管理，不返回给调用者。

收回 parent 原本持有的权限时，下级若仍保留对应授权，服务端拒绝并指出成员及权限，先自下而上收回，再收回 parent；不自动级联修改或删除业务服务。用户独立授予、位于本次收窄范围外的权限不阻塞。包括已归档成员在内，授权仍保留，须先恢复并调整。权限待生效或配置暂停恢复期间不能据此转授，上级收窄也会等待下级变更完成，防止失败回滚后下级仍保留新授权。

GPU/沙箱沿用步骤边界在线应用及失败回滚；systemd 授权即时影响后续调用，已启动服务不自动停止；额外 MCP 按上文条件在线应用或正常暂停恢复原 Thread。root、关闭沙箱、systemd 是宽权限通道，单独关闭 GPU 或收窄路径不限制其他已获准通道的实际能力。

## read/write/deny 与转授

规则形式为 `{path: 绝对路径, access: read|write|deny}`。workspace 默认可写，未匹配路径规则的文件禁止访问。`/=read` 表示其他宿主文件可读，可由用户编辑或删除；系统私有数据仍受保护。

普通规则按路径更具体者优先，同一路径 deny > write > read；例如 /project=deny 与 /project/app=write 可同时存在。创建 child 时，只能转授自己显式拥有的权限或自己 workspace 中的相同/更窄权限，可以连同嵌套限制一起转授宽目录，也可以只转授已开放的子目录；不能遗漏限制来扩大权限，read 不能变成 write。平台控制目录、其他未共享 workspace 等不能由 grants 重新授权。

修改直接 child 的路径权限时，上级可以在自己权限范围内添加、收窄或撤销授权；范围之外的已有权限保持原样，不要求上级重新拥有这些权限。例如 child 已有用户给的 /finance=write，parent 只有 /garden=write，parent 可以保留 /finance 并调整 /garden 下的收发区。仍提交完整 grants 列表，不能遗漏无权修改的原授权。系统按整套规则比较修改前后的权限变化，包含删除 deny 导致的重新开放；只读权限不足以撤销或收窄已有可写权限。保留已有可写路径不会让 parent 获得在范围外准备缺失目录的权限。

收窄 parent 时，若实际收窄的区域仍有 child 持有更高权限，先收窄 child；child 在其他区域持有用户独立给的权限，不阻塞此次修改。权限待生效期间的转授与修改仍受下文的并发检查约束。

路径必须绝对、不含 `..`、不经过符号链接。write 目标不存在时由宿主按目录准备，不授予其父目录写权限；read/deny 目标必须已存在。删除授权只撤销权限，不删除文件。一般宿主的“只读目录”本身不能由成员在其中创建文件夹；需对已存在子目录授权，或通过可准备新目录的 write 授权由宿主创建目标。

## 网络与进程

当前文件沙箱不是网络目的地或监听端口 allowlist；其 network 为 unchanged。能写应用代码不代表能改共享网关；能绑定空闲端口不代表 Windows、路由器、DNS、TLS 或公开代理已配置。后台进程也不自动等于长期服务。

Harness 为指向隐藏 WSL 桥接目录或 /run 的系统 DNS 配置提供单文件只读视图，不需要给成员开放整个宿主目录。显式 deny 和私有根保护仍优先。联网失败时应分别检查域名解析、目标 TCP 连接和 HTTP/TLS 响应，不能把 DNS 失败直接认作缺少联网授权；详细机制见 aster-harness skill 的文件沙箱指南。

宿主 IPC、Unix/vsock、新进程可见范围等有独立沙箱边界。增加一个可写目录不能使被阻断的 systemd 通道可用。涉及本机部署时查私有部署指南并联系对应管理者，明确需要的是文件、应用进程、服务托管还是入口接入能力。


## 宿主用户级 systemd

先通过 `squad_self` 查看 `agent.config.host_systemd`；缺省或 false 表示无权。开启后发现 `squad_host_systemd`，参数 program 取 systemctl、journalctl 或 systemd-run，arguments 是字符串数组，工具自动添加 --user，不经过 shell。普通程序执行仍在原有沙箱中；本工具在宿主当前 Linux 用户身份运行，服务不会自动继承 agent 的路径沙箱。

这是一项完整的宿主用户级服务管理权限，没有服务名称 allowlist。可创建或修改服务、运行宿主程序，也可能访问原本被沙箱隐藏的路径。路径 grants 不再是此成员的完整隔离边界。用户或持有该权限的直接 parent 可以开关，创建 child 默认不开启；撤销后立即拒绝新调用，但已经启动的服务继续存在，不自动停止或删除。

示例（均为工具 arguments，不含 --user）：

- 状态：program="systemctl", arguments=["status", "example.service", "--no-pager"]。
- 安装项目中的 unit：arguments=["link", "/绝对项目路径/example.service"]；随后 ["daemon-reload"]、["enable", "--now", "example.service"]。unit 及引用文件须在 agent 停止后仍存在，不依赖沙箱内合成路径。
- 日志：program="journalctl", arguments=["-u", "example.service", "-n", "100", "--no-pager"]。
- 临时常驻任务：program="systemd-run", arguments=["--unit=example", "--", "/绝对路径/程序", "参数"]。不使用 --scope；该方式不等于安装重启后自动启动的持久 unit。

工具只允许当前宿主用户的 manager，不提供 root、切换主机或 Windows 管理权。每次执行超时 1–120 秒，输出各最多 64 KiB；长期程序应作为服务启动，不用 journalctl -f 等持续占用调用。审计记录 actor 与程序名，不记录可能含敏感信息的命令参数或日志。实际公开入口、端口与运维分工仍参考部署环境的 skill 和用户约定。


## 沙箱开关与保存规则

用户可以在创建或编辑成员时设置 `sandbox_enabled`，默认 true；旧成员未保存该字段时同样默认开启。parent 只有在自己的 sandbox_enabled=false 时，才能关闭或重新开启 child 的沙箱；不能修改自己。child 默认开启，不自动继承 parent 的关闭状态。root 身份使沙箱暂不生效，不代表获得关闭沙箱的转授权。

开关独立于 grants、模型、instruction、宿主 systemd 授权等配置。关闭时这些配置保持原值，普通本机命令不再经过 bubblewrap、PID 隔离和 seccomp，以宿主当前用户身份运行。保存的 read/write/deny 与平台保护路径在关闭期间不构成操作系统隔离，也不因此取得 root 或 Windows 管理员权限。

重新开启时，系统按原有 grants 加上当时的平台保护路径恢复隔离。关闭期间也可以编辑将来要用的路径规则。仅切换沙箱或修改路径规则时，在下一执行步骤边界在线替换，保持同一个 Thread、Session 和 worker；旧命令及长代码 cell 会停止，service 发生器从入口重启，once 操作不重放，独立 systemd 服务不受此开关自动启停。

`agent.config.sandbox_enabled` 表示保存的开关（缺省 true），`agent.sandbox_enforced` 表示当前空间是否实际应用；演示空间始终不运行真实沙箱。独立的 host_systemd 授权若仍开启，重新启用普通命令沙箱不会撤销这项宿主执行通道。


## 在线权限生效状态

`config.grants` 与 `config.sandbox_enabled` 是用户已保存的目标配置。`permission_update.pending=true` 时，目标还没有被运行时确认，实际权限仍看 `filesystem.effective_grants`、`sandbox_enforced` 和 Harness 当前的环境版本。待当前模型请求及工具批次结束，宿主通过公开 Agent.set_filesystem_policy 提交的策略在 Step 边界生效；收到匹配请求 ID 的运行时回执后，status 变成 applied。候选探针失败则 status=failed，并恢复原权限字段；不会把入队当成功。

新策略会结束旧命令和运行中的代码 cell，并重连受沙箱控制的 MCP、从入口恢复常驻发生器。命令与一次性操作不自动重放，已发生的副作用不能假定被撤销。独立宿主 systemd 服务仍由它自身的权限与生命周期管理。

等待期间禁止重复提交权限更新、基于该成员创建 child 或转授权限；上级收窄前也会检查下级是否有待应用变更。请求及目标配置保存在控制目录，离线/恢复不会默默丢掉；恢复时先按已保存目标配置启动，再确认生效。修改模型或身份仍使用正常暂停恢复；若同时修改这些字段，整次走该流程。

## 平台默认隔离

平台整体隐藏共享数据根，再为每位成员预设自己的 workspace 可写、连接目录只读、必要运行目录只读。其他成员和预留槽位不逐项展开，创建新成员不会向旧进程暴露其私有目录。Harness 继续固定保护 Thread 存储及认证等内容，必要程序状态只通过专用接口和合成路径提供。平台控制、认证、未共享成员目录不能作为普通 grants 授权；旧版共享 workspace 内的 .connections/.relay 仍受保护。工作台区分自定义规则、系统隔离区和系统预设例外。

收发区可用两条普通规则表达：先给 /project/exchange=deny，再给 /project/exchange/本人=write。这会隐藏其他人的分支，也不会给本人其父目录写权限。交付者的可写权限、凭据保存约束仍须分别满足；目录分格本身不代表保密。

## root 身份

`agent.config.run_as_root` 默认 false，仅用户可在工作台授予/撤销，不继承给 child。开启后原有工具在宿主 root worker 内运行，不另设专用命令工具；可管理整个 Linux/WSL，不包括 Windows 管理员。路径沙箱暂不生效，保存的 sandbox_enabled 和 grants 不变，关闭后恢复。通过正常暂停/恢复切换身份，保留同一 Thread；已经完成的系统改动不自动回滚。新建项目文件可能归 root 所有，需按项目要求管理属主；平台只交还成员内部 runtime 的属主。

宿主须先安装固定启动器，详见部署文档。root 不能由 parent 转授，也不能从可写路径或用户级 systemd 授权推断出来。共享宿主账号和 Squad 安装环境属于可信宿主边界；root 成员本身能够越过其他成员的数据保护。

## GPU 设备

用户可在成员配置开启 `gpu_access`，当前支持 WSL2 `/dev/dxg`。持有 gpu_access 的直接 parent 也可授予或收回，不自动传给 child；没有 GPU 时拒绝启用。`squad_self` 的配置给出已保存授权，`filesystem.gpu_access` 给出沙箱有效状态；null 表示当前不应用沙箱。普通路径 write 不包含设备授权。

创建、保存、在线更新和失败回滚沿用现有权限机制，GPU-only 变更不重启 worker、保留 Thread；更改在当前步骤结束后生效，旧受管命令会结束，本地服务及常驻监听按新策略重建。关闭沙箱或 root 身份期间不限制宿主设备，GPU 设置仍保留供重新开启沙箱使用。权限不附带显存配额或资源独占。训练前检查所用 Python 环境和 CUDA，优先盘点已有依赖；GPU 授权本身不要求安装新环境。

## Windows 原生成员

Windows 使用相同的默认禁止路径规则，`/` 表示跨本地盘符的根规则，具体盘符和子目录可覆盖它。Windows 管理员模式 windows_admin 仅用户可设置、不可转授；开启期间保存路径配置但不应用 MXC。Linux root、systemd 和 WSL GPU 不自动映射为 Windows 能力。Windows 安装、UAC、MXC probe/smoke 和独立数据目录见项目 WINDOWS.md。

## 宿主管理员的一致语义

Linux 的 run_as_root 与 Windows 的 windows_admin 是同一类用户授予的宿主管理员能力，分别通过已安装的 sudo 启动器和 UAC 实现；不向下转授。只有用户可修改这些成员的配置、启动成员或重置其聊天凭据，普通 parent 仍可正常暂停。提权期间普通路径沙箱暂不生效，但保存的 sandbox_enabled、grants 与 GPU 选择仍可由用户编辑，退出提权后按保存值恢复。Linux 与 Windows 不共享管理员身份、数据目录或系统服务机制；systemd 与 WSL GPU 不映射为 Windows 能力。


## 本机进程通信

Linux / WSL 真实空间可配置 `local_ipc`，缺省 false。工作台“允许本机进程通信”和 `squad_create` / `squad_configure` 使用相同授权。显式持有该能力的直接 parent 可以转授和收回，不能从 root 或关闭沙箱推断转授权；先收回下级，再收窄上级。

开启后普通沙箱命令可使用 Unix socket，默认得到执行树独立的 `/tmp`、`/var/tmp`；临时内容不跨命令保留，持久文件放工作区。非根祖先或同路径显式规则优先，子路径授权仍有效。应用及字体等依赖另需可读。此能力不授予 root / systemd 工具；但可连接已暴露且宿主用户有权访问的服务，服务可能越过文件授权代为操作，只读不禁止 socket 请求。并不保证只与自己的子进程通信。`/run`、私有控制目录和 WSL 桥接继续隔离，AF_VSOCK 仍禁止。

保存值见 `agent.config.local_ipc`；有效值见 `agent.filesystem.local_ipc`，null 表示不使用沙箱。开启 root 或关闭沙箱期间保留设置。在线修改沿用步骤边界、失败回滚及原 Thread，不自动重放命令；受管进程的临时目录随旧进程结束而消失。
