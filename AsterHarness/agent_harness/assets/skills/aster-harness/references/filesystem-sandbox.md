# 文件系统沙箱

基础版采用 Codex 的三档路径权限：`read` 可读不可写、`write` 可读写、`deny` 不可读写。工作区默认 write，未匹配路径规则时 deny；可编辑或删除 `/=read` 来控制全局只读范围，仍受 Linux 原有权限约束。这里没有审批、模型提权工具或失败后无沙箱重试。初期支持 Linux/WSL2，其他平台开启时明确报错。

## 启用与配置

需要系统 `bubblewrap` 和 `libseccomp.so.2`，以及允许创建用户、挂载和 PID 命名空间的内核环境。Ubuntu/Debian 可安装 `bubblewrap libseccomp2`。启动探针或执行器失败不会退回普通宿主执行。验证环境：WSL2、bubblewrap 0.4.0、Python 3.11。

新建 CLI Thread 默认启用：

```bash
python -m aster --workspace /path/to/workspace \
  --read-only-path reference \
  --deny-path private
```

路径参数可重复使用，额外写入目录使用 `--writable-root`。也可用 `--filesystem-policy examples/sandbox/policy.json` 提供 JSON，不能与上述路径参数混用。示例中的 reference、private 必须事先存在。规则中的相对路径以固定 workspace 解析，执行命令中的 cd 不改变权限根。`--sandbox off` 是显式的宿主配置；不能与路径规则同时使用。

SDK 创建方式：

```python
configuration = SessionConfiguration(filesystem_policy={
    "rules": [
        {"path": "reference", "access": "read"},
        {"path": "private", "access": "deny"},
    ]
})
session = manager.create(client, workspace, configuration=configuration)
```

为兼容既有宿主和 Thread，SDK 的 None 表示未开启；CLI 新建入口显式传入默认沙箱策略。旧 Thread 的 resume 使用原来保存的设置，不会默默改变正在使用的实例。

规则最多 64 条。第一版要求路径已经存在，不接受符号链接或穿过符号链接的规则路径；也不支持 glob。路径失效时拒绝执行，不忽略禁止规则。所有普通路径规则按更具体的路径优先；同一路径使用 deny > write > read。父目录 deny 下可以显式开放更深的 read/write，开放区内也可继续收窄。重复路径按这一顺序合并。宿主固定私有目录另行保护，普通规则不能重新开放；受保护的运行时代码和 Python 环境只有只读上限，不因此穿过 deny 获得读取权。只读路径上的删除、替换、重命名也受限制。

`deny` 不等于隐藏名字：父目录可读时，禁止项的名字、类型或遮罩属性仍可能可见，但真实内容被隔离。这是路径策略，不是内容追踪；已经读取的内容、历史、复制件、预先存在的硬链接等不因此被撤回或统一打上保密标签。

## 系统执行边界

FilesystemSandbox 统一构造执行请求；可信 Python launcher 在干净环境中启动 bubblewrap。根目录默认遮罩，只保留显式路径、workspace 和系统运行例外；显式 `/=read` 恢复全局只读视图；显式设置 `/` 为 write 时先建立可写根视图，再建立隔离的 `/proc`、`/dev`、`/run`，不以根挂载覆盖这些内部视图。普通规则按父路径到子路径顺序挂载，不按模式分批处理。deny 文件用零权限空文件遮罩；deny 目录只为显式开放的子路径准备挂载点，中间目录仅允许穿行、不允许列目录和写入，其他原始内容不进入视图。宿主固定禁止路径最后遮罩。挂载源逐级通过 O_PATH/NOFOLLOW 描述符固定，避免父目录符号链接改变来源。

使用用户、PID、IPC 等命名空间、独立会话、能力移除和 seccomp。seccomp 拒绝重新挂载/切换命名空间、ptrace/跨进程内存访问、io_uring 和新建 AF_VSOCK 连接等逃逸入口；AF_UNIX 默认禁止，可显式授权；socketpair 仍可用于进程内部协作。

允许 `unshare(CLONE_NEWUSER)` 创建内部用户命名空间，以兼容程序自身的沙箱；其他 unshare 标志仍拒绝。内部 UID 0 不拥有宿主或外层命名空间的 root 权限。沙箱私有 `/proc` 允许进程写入自己的 UID/GID 映射；`/proc/sys`、`/proc/fs`、`/proc/irq`、`/proc/bus` 只读，`sysrq-trigger` 被只读空设备遮罩。新旧挂载 API、setns 和跨进程逃逸接口继续禁止，路径挂载与 seccomp 限制由后代继承。内部身份可能读到 deny 文件的空遮罩，不会得到原文件内容，也不能替换遮罩。

这不自动授予本地 IPC。需要 Unix socket 的应用仍须显式开启 `local_ipc`，并将缓存/配置写入获准的可写目录。例如 Chromium 可保留自己的沙箱，通过 stdio MCP 在成员沙箱内运行；不需要 root、关闭沙箱或开放宿主缓存目录。权限撤回仍终止旧执行树，包括脱离进程组但留在沙箱 PID 命名空间内的后代。

将 `/` 配为 write 会使普通主机路径广泛可写，但 deny、可信运行时只读挂载、私有目录隔离和 Linux 原有权限仍然有效，不等于关闭沙箱。

这是文件系统沙箱，IP 网络继续可用，包括本地 HTTP 网站。没有域名白名单或联网审批。Unix socket 受限是为了避免通过 Docker、宿主代理或 WSL 互操作绕过本地执行边界。`/proc` 呈现沙箱进程；`/dev` 只提供基础设备；`/run` 是隔离的运行时目录，`/dev`、`/run` 以只读视图收束（显式运行时子目录除外）。GPU 设备需独立显式授权，详见后文；不开放其他额外设备。

DNS 配置是必要的只读运行设施。若 `/etc/resolv.conf` 是指向被隐藏的 WSL 桥接目录或 `/run` 的符号链接，launcher 只把其实际目标文件只读挂载回同一位置，使原链接可用；不开放宿主目录、相邻文件或 Unix socket。每次启动命令重新固定目标文件，宿主更换配置后新命令使用新配置。普通策略对配置路径或目标的显式 deny、宿主私有根保护仍然优先；宿主原本缺失的配置不会被伪造。这不是 DNS 服务器、联网代理或搜索工具，也不增加进站端口权限。

命令、TTY、子进程、自定义脚本、程序发生器、发生器测试、本地 stdio MCP 都经过此边界。read_file/list_directory/apply_patch 在沙箱工作进程中执行，图片读取也走同一边界。Skills 的目录扫描、专用加载和读取为宿主能力，不受文件沙箱限制；skill 脚本仍通过普通命令工具在沙箱内执行。read_file 在启用时支持工作区外的允许路径；apply_patch 继续保留原有工作区路径限制。

MCP SDK 仍负责协议，stdio 进程通过同一个 launcher 启动；明确配置的 MCP 环境变量是宿主授予的额外能力。HTTP MCP 的远程服务权限由该服务负责，本地沙箱不能限制远端机器。宿主自行注册的 Python handler 属于可信宿主代码，不会自动被搬进子进程；需要文件隔离的 handler 应使用统一执行接口。

## 私有状态与环境变量

普通执行进程不可访问 Thread 存储根、默认项目模型 `.env`，以及 WSL 主机互操作路径。框架代码和 Python 运行环境只读。专门的记忆、历史、附件和工具输出接口仍由宿主提供受限访问，不向普通命令开放私有目录。其他凭据文件应显式配置 deny；显式 `/=read` 会开放其他宿主文件，不能用它隐藏密钥内容。

子进程环境使用小型白名单，不直接复制宿主 OPENAI_API_KEY 等变量；可信 launcher 使用 Python -I -S，避免用户启动钩子。实际业务命令仍可执行其获准读取的启动文件。

自定义工具只得到它的只读脚本快照，不得到私有注册表。程序发生器只得到自己的可写状态目录与事件通道：状态目录映射至 `/run/aster-harness/state`，已有事件连接经 stdin 的专属描述符提供，SDK emit_event 继续返回正常确认。普通命令无该通道，不能仅凭导入 SDK 发送事件。发生器自己的私有状态与内部日志不通过普通文件路径开放；测试工具提供测试输出，业务日志可写入工作区。

## 运行中修改

只有宿主 API，模型不能自行放宽策略：

```python
request_id = await agent.set_filesystem_policy({
    "rules": [{"path": "private", "access": "read"}]
})
```

这是完整替换，并非追加。请求先作为事件持久化，下一个 Step 边界验证并应用，当前模型请求及工具批次先结束。公开 Agent SDK 与底层 Session 均支持该宿主接口；传 None 可关闭，传 rules 对象可启用或替换。返回值是入队 ID，不代表应用完成；原生 applied/failed 回执带 request_id，同一步合并时另列 superseded_request_ids。profile 宿主需同步保存 thread.toml 的目标 sandbox 配置，供下次 resume 使用。

候选启动探针失败时保持旧策略和进程。成功后取消长 Code Mode cell，停止旧命令，关闭旧 MCP 连接，暂停程序发生器；持久化新配置和递增 version，再启用新策略、连接 MCP、重启 service 模式发生器。once 模式不重放，报告中断。已接受的旧事件不会被丢弃或变成新的授权。

旧命令句柄不再有效，命令和 cell 不自动重试。可能产生的副作用通过 outcome_unknown 提示；服务从入口恢复不等于业务操作天然幂等。服务或 MCP 后续启动失败会报告失败，新权限不会因此回退。框架持久化失败则停止 Session，不继续宣称更新完成。

## WorldState 与模型上下文

新增内置 environment section，靠近其他 WorldState 部分呈现，采用 Codex 风格结构：

```xml
<environment_context>
<filesystem policy_version="2">
<workspace_roots><root>/path/to/workspace</root></workspace_roots>
<permission_profile type="managed">
<file_system type="restricted" default_access="deny">
<entry access="read"><special>:root</special></entry>
<entry access="write"><path>/path/to/workspace</path></entry>
<entry access="deny" escalatable="false"><path>/path/to/workspace/private</path></entry>
</file_system>
</permission_profile>
</filesystem>
...
</environment_context>
```

只列工作区、例外与受保护路径，不枚举整个硬盘。上下文区分普通规则的更具体优先、同路径 deny > write > read、不可绕过的宿主禁止目录以及运行时代码只读上限，同时说明无审批和无自动降级，以及最高 policy_version 生效。

初始权限是固定基线，实际更新成功后追加新的环境项，不重写原来的前缀。整体 compact 将最新状态提升为基线；局部 compact 保留精确位置检查点。策略配置、version 和投影均持久化，resume 恢复执行限制，不只恢复提示词。内部状态字段不发送给模型，模型输出不能伪造权限更新。待处理的更新请求也能随干净 suspend/resume 恢复。

本实现参考上游 Codex c419fd0：sandboxing/src/manager.rs、linux-sandbox/src/bwrap.rs、linux-sandbox/src/seccomp.rs、core/src/context/environment_context.rs。保留本项目无 turn、事件驱动和工具回执队列设计。

## 可选关闭沙箱

宿主可在单个 Thread 的 `thread.toml` 中设置：

```toml
[sandbox]
enabled = false
rules = []
```

修改文件是配置草稿；执行显式 reload 或下次 resume 后生效。已有 Thread 可以关闭、重新开启沙箱，保留历史、记忆、工作目录及递增的权限版本。SDK/宿主运行时也可以调用 `await agent.set_filesystem_policy(None)` 关闭，传入 `{"rules": [...]}` 重新开启；操作在下一 Step 边界生效，不向模型提供更改权限的工具。

关闭时沿用权限替换流程：停止旧命令和 Code Mode cell，重连 MCP，重启已注册的 service，once 操作不重放。WorldState 追加 `permission_profile type="disabled"` 的新环境上下文；compact 后由新的基线承载，恢复时保持关闭状态。默认不使用沙箱的旧 Thread 仍保留原有上下文结构。

关闭意味着不再使用 bubblewrap、PID 隔离和 seccomp；本地命令以宿主进程的用户身份运行，可以按 Linux 原有规则访问文件及操作其他进程。同一 UID 的其他进程通常可被发信号，但不会因此获得 root、其他用户或 Windows 管理员权限。沙箱的私有路径隔离随之取消；记忆仍通过专用工具维护的规则属于 agent 指导，而非关闭状态下的 OS 防护。

## 子目录例外与授权校验

```python
rules = [
    {"path": "/project", "access": "deny"},
    {"path": "/project/app", "access": "write"},
    {"path": "/project/app/reference", "access": "read"},
]
```

app 内可读写，reference 内只读，project 其余内容不可访问；父目录只保留进入例外目录必需的穿行能力。更深的 deny 仍会生效。Linux 本身的权限继续适用。

宿主可以用公开 `aster.permissions.access_for(path, rules, default="deny")` 查询普通规则，路径须已规范化为绝对路径。`covers(granted, requested)` 比较两套显式授权，未覆盖位置按 deny 处理；会检查所有路径边界，允许保留限制的宽目录转授，拒绝通过遗漏嵌套限制扩大权限。这两个纯函数不代替宿主固定保护校验或实际沙箱探针。

普通规则可将 `/` 设为 deny，但必须明确保留启动程序所需的系统/解释器路径，否则启动探针会拒绝配置。合成的 /proc、/dev、/run 仍由运行时控制；它们不开放宿主对应的进程或 IPC 视图。

默认禁止时，宿主为执行保留系统程序、共享库、Python 环境和单个 DNS 配置文件。若本地 MCP 或脚本依赖安装在其他目录（例如用户级 site-packages），应明确配置该依赖目录的只读授权，或安装到受支持的运行环境，不能通过开放整个 home 规避。系统运行路径是明确的只读例外；更具体的 deny 和不可覆盖的私有根保护仍生效。

## GPU 设备授权

GPU 是独立设备能力，不由普通路径 write 授予。当前支持 WSL2 的 `/dev/dxg`；`SessionConfiguration(filesystem_policy={"rules": [...], "gpu": True})` 或 `[sandbox] gpu = true` 显式开启，缺省关闭。设备缺失或与显式 deny/私有路径冲突时拒绝启用，不自动离开沙箱。launcher 保留精简 `/dev`，校验并固定字符设备描述符，只用设备绑定开放该节点；普通 `/dev`、`/proc`、`/run` 授权仍被拒绝。

模型环境上下文的 `gpu_devices` 列出当前获准设备。GPU 变更沿用 `Agent.set_filesystem_policy` 的步骤边界与成功/失败回执，停止旧受管命令、重建本地工具服务和常驻事件源；不会重放一次性工作。撤销通过结束持有设备句柄的受管进程生效，不能只隐藏路径。宿主 systemd/root 等绕过沙箱的执行不在此限制内。权限不表示显存配额、任务调度或资源独占。

Python 解释器与依赖另行选择；授权 GPU 不安装 PyTorch，也不修改模型、项目环境或文件授权。原生 Linux NVIDIA 设备与原生 Windows GPU 不在本版支持范围。

## 私有共享内存

每次沙箱执行都在只读 /dev 下挂载独立可写的 /dev/shm tmpfs，保留 IPC 隔离，不绑定宿主共享内存。它是 OpenMP、NumPy/PyTorch 和 POSIX shared_memory 等程序的基础运行环境，不依赖 GPU 或路径授权。该执行树中的子进程共享此目录，其他沙箱执行和宿主不可见；最后一个持有该挂载的进程退出后释放，不跨命令持久化。/dev 其余位置仍只读，设备使用仍须独立授权。


## 本机进程通信（Linux / WSL）

`filesystem_policy={"rules": [...], "local_ipc": True}` 或 `[sandbox] local_ipc = true` 开启 Unix socket 创建、监听及连接，缺省关闭。它是独立能力，不授予 root、GPU 或 systemd 管理工具。Windows 当前拒绝此选项，不能把 Linux 语义视为 MXC 的保证。

开启时默认把 `/tmp`、`/var/tmp` 替换为每次执行树独立的可写临时文件系统；不暴露宿主同名目录的内容，也不在命令之间持久保存。子进程共享其父命令的临时目录，适合 LibreOffice 内部通信。需要跨命令保存的文件或 socket 放在获准的工作区。显式非根祖先或同路径规则优先；显式子路径授权从宿主绑定到对应位置。若明确授权宿主 `/tmp` 可读写，将按该授权暴露宿主目录。保护路径仍最后屏蔽。

此能力允许连接路径可见且宿主用户有权访问的 Unix socket，包含可能代为执行越过文件权限操作的服务；只读路径不等于禁止 socket 请求。抽象 Unix socket 也可能可达，所以它不是“仅自己子进程通信”隔离。`/run`、WSL 桥接、私有控制目录的原有保护不变，AF_VSOCK、io_uring 仍禁止。

通过公开 `Agent.set_filesystem_policy` 在步骤边界更新；已有命令会停止，受管监听重新启动，一次性操作不重放，保留原 Thread。权限失败仍使用原策略。文件可读写、可执行程序及字体等运行依赖需要另外满足；仅开启 IPC 不会自动安装应用。
