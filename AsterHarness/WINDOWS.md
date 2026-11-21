# Windows 原生运行（本地预览）

Windows 11 可以显式启用 MXC 沙箱预览，默认支持联网，未匹配路径默认禁止，可用显式 `/=read` 根规则开放全局读取。[Windows 文件沙箱验收记录](docs/design/windows-filesystem-sandbox.md)

本次适配增加 Windows 后端，Linux 继续使用原有实现。需要原生 Windows Python 3.11+；本轮实际验证为 Windows / Python 3.12。不要使用 WSL Python 创建 Windows 虚拟环境，也不要复用 Linux 的 .venv。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = '1'
python -m pip install -e .
```

依赖从各仓库源码安装；私有仓库需要自己的读取身份。pip 不配置跨账户的凭据。各组件运行时不要求源码目录相邻。

使用新的 Windows 数据目录，避免同时打开 WSL 原有数据库和 Thread。Windows 文件锁、UTF-8 文本、进程启动和退出有独立适配；不安装或修改 WSL 服务、Windows 服务、全局代理或防火墙。

## 可运行的范围

支持显式无文件沙箱的 CLI/SDK、常驻事件循环、工具、记忆和压缩、正常暂停/恢复、程序发生器、MCP、Code Mode，以及 PowerShell 非交互命令、后台收集和停止。后台进程由 Windows Job Object 管理；非 TTY Ctrl+C 在此预览版中终止该命令树，不模拟 POSIX SIGINT 的可捕获语义。

在通过原生 MXC 探针的 Windows 11 上，可以给 Harness 宿主进程设置 `ASTER_MXC_EXECUTOR`，值为微软签名的 `wxc-exec.exe` 的绝对路径。需要 PowerShell 7 时可同时设置 `ASTER_MXC_POWERSHELL`。MXC 预览适用于自建的本地工作区，并沿用成员配置中的读、写、禁止规则。已有硬链接的限制见[沙箱设计文档](docs/design/windows-filesystem-sandbox.md)。

未配置 MXC 的 Windows 主机要求可信开发显式使用 `--sandbox off`，或在 SDK 中传 `SessionConfiguration(filesystem_policy=None, max_consecutive_steps=None)`。关闭文件沙箱后，工具拥有宿主用户权限。

```powershell
python -m pip install -e '.[mcp,code-mode,chatgpt]'
python -m aster --demo --sandbox off --workspace examples/workspace
python scripts/test_windows.py
```

真实模型仍使用原有 model/env 配置和 ChatGPT 登录入口。自动回归使用 ScriptedClient；2026-11-04 另外完成了原生 Windows 真实模型验收：ChatGPT OAuth + `gpt-6.1-sol`，通过 SDK 修改独立测试项目、运行 5 项 unittest，并在关闭后恢复同一 Thread 再次通过测试。该验收显式关闭文件沙箱，未复用已有 Linux Thread。

### 当前边界

- `tty=true` 明确不支持；尚未实现 ConPTY。普通命令和后台进程可用。
- 默认 shell 为可找到的 pwsh.exe / powershell.exe。PowerShell 中解释器变量是 `$env:HARNESS_PYTHON`，调用路径使用 `& $env:HARNESS_PYTHON ...`；不要复制 Bash 的变量语法。
- Windows 定时器使用最长 250 ms 的墙钟重检，支持取消和过期恢复；这是预览实现，Linux timerfd 完全保留。未来可替换为 Windows 内核定时器。
- 程序事件通过随机本机 TCP 端口及每个发生器的随机令牌传递，普通 exec 不继承事件通道；没有开放远程监听。
- 凭据文件使用当前账号和 LocalSystem 的受保护 DACL，不能把 chmod 当作 Windows 文件沙箱。
- 此轮不迁移已有 Linux Thread，不保证含 Linux 绝对路径、脚本或权限的历史可直接跨系统恢复。
- 不支持把 UNC、junction 或符号链接当作私有 skill 资源入口。

完整 Linux 回归仍使用 `python -m unittest discover -s tests -v`。Windows 的阶段性验收入口选择当前支持的功能；原完整测试还含 Linux 沙箱、/proc、Unix 信号、Bash 和权限位断言，不能把这些功能的失败当作 Windows 已支持。

模型名与端点从宿主配置读取，密钥通过配置引用的环境变量传入。离线回归使用 fake/ScriptedClient，验证真实模型时可在宿主配置中选择 DeepSeek 或 ChatGPT OAuth。

2026-11-04 DeepSeek 补充实测通过：原生 Windows 的 `deepseek-flash` 单 Agent 修复及同 Thread 恢复、两成员协作开发与独立审核均完成；各次 5 项验收测试通过，团队服务正常退出。
