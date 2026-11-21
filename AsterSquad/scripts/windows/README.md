# Windows 本地工作台

在 Windows PowerShell 中创建并激活一个原生 Python 虚拟环境。以下示例假定三个源码目录位于同一父目录，实际安装位置可以不同。所用检出版本需要包含 Windows 支持。

```powershell
py -3.12 -m venv .venv
. .\.venv\Scripts\Activate.ps1
python -m pip install -e './AsterHarness[mcp,code-mode,chatgpt]' -e './Relay' -e './AsterSquad[windows-window]'
Set-Location ./AsterSquad
```

原生应用窗口需要 Microsoft Edge WebView2 Runtime。若要启用生图，再安装 AsterImages，并在已停止的实例配置里设置 `images_enabled`、原生 Codex 可执行文件路径及其登录目录。

## 初始化与启停

初始化可以先不配置模型凭据；启动前需提供 API key 或在 desktop.json 配置自己的 ChatGPT 账号。默认 API 示例使用 DEEPSEEK_API_KEY，也可用 ASTER_STUDIO_API_KEY_ENV 指定别的模型凭据环境变量，并配合 ASTER_STUDIO_MODEL、ASTER_STUDIO_MODEL_URL 和 ASTER_STUDIO_MODEL_ID 配置模型。密钥用 Windows DPAPI 存在数据目录。

```powershell
.\scripts\windows\aster.ps1 init
.\scripts\windows\aster.ps1 start
.\scripts\windows\aster.ps1 window
.\scripts\windows\aster.ps1 window -Page relay
.\scripts\windows\aster.ps1 status
.\scripts\windows\aster.ps1 stop
```

启动器使用当前 PATH 中的 Python，或使用 `-Python <python.exe 路径>`。也可通过 `python -m squad.windows_desktop <action>` 调用。默认数据目录是 `%LOCALAPPDATA%\AsterWindows`，默认工作台端口为 21910，只监听本机。可用 `-Root` 选择其他实例，并为各实例配置不同端口。关闭窗口后服务继续运行。

配置 DeepSeek 凭据后，工作台的模型目录会包含 DeepSeek。要启用 ChatGPT 登录，运行 `python -m aster auth login --home <目录> --profile <名称>` 并完成浏览器授权。停止实例后，把登录目录和名称写入 `desktop.json` 的 `chatgpt.home` 与 `chatgpt.profile`，再启动。模型列表会包含 `gpt-6.1-sol`，实际访问权限由账号决定。

## MXC 沙箱

使用微软签名的 `wxc-exec.exe`。先验证本机的原生沙箱能力，再运行实际隔离测试。

```powershell
.\scripts\windows\aster.ps1 sandbox-mxc-probe -MxcExecutor 'C:\tools\mxc\wxc-exec.exe'
.\scripts\windows\aster.ps1 sandbox-mxc-smoke -MxcExecutor 'C:\tools\mxc\wxc-exec.exe'
.\scripts\windows\aster.ps1 start -MxcExecutor 'C:\tools\mxc\wxc-exec.exe'
```

要让后续启停和窗口快捷方式沿用此配置，在实例停止时将执行器绝对路径保存到 `desktop.json` 的 `mxc_executor`。启动状态会显示 `sandbox_backend: mxc`。MXC 工作台保护工作台数据目录和 Codex 数据目录。可在 `mxc_private_roots` 列出其他保护路径。

启用成员的「沙箱」后，未匹配路径默认禁止，显式 `/=read` 表示全局只读；工作台可直接编辑或删除该根规则。所有规则由 MXC 执行。第三方 stdio MCP 的脚本和运行时需要在该成员的可读范围内。使用可信工作区，留意已有硬链接的 [隔离边界](https://github.com/WayneW1119/AsterHarness/blob/main/docs/design/windows-filesystem-sandbox.md)。

## 成员管理员模式

新建或编辑 Windows 真实成员时可以勾选「以 Windows 管理员身份运行」。首次启动或需要更换进程身份时由 UAC 确认。成员以管理员身份运行期间暂停 MXC 隔离。原沙箱开关和路径规则保留，关闭管理员模式后恢复。

已在线的成员在配置需要更换进程时会正常暂停，并自动恢复同一条 Thread。拒绝普通成员的提权请求时，会尝试按原配置恢复。新建时拒绝 UAC 会保留成员，稍后可手动启动。原本已暂停的成员只保存配置。

可使用 `python scripts/test_windows.py` 运行 Windows 回归。

更新时先停止 Windows 实例，AsterHarness、Relay、AsterImages（如使用）、AsterSquad（以及启用时的 AsterWeb）更新到相互兼容的 main 版本，再在同一个虚拟环境重新执行安装命令。先运行 Harness 与 Squad 的 `scripts/test_windows.py`，再运行 MXC probe/smoke、创建成员、普通/管理员切换及原 Thread 暂停恢复验收。请使用独立测试数据目录。GPU 本版仍只支持 WSL2，Windows 原生 GPU 不在本轮支持范围。

新初始化实例默认不启动搜索或生图组件。需要时独立安装 AsterWeb/AsterImages，在实例停止时设置 desktop.json 的 web_enabled/images_enabled=true，再启动；未启用组件不检查其包或端口。旧版配置未写 web_enabled 时保留其原有搜索启用行为，可显式设 false。组件凭据不写入源码或普通 JSON，仍由当前 Windows 账号的 DPAPI 保存。

初始化时已有的模型 key 会保存在 DPAPI 中；也可在启动会话通过配置的 api_key_env 提供，运行时环境中的模型 key 优先于已保存值，且不会自动写回凭据文件。

空间设置可选「空间启动后恢复成员」，默认关闭。开启后只恢复仍请求运行、未归档且无错误的成员；手动暂停后不自动启动，失败不循环重试。自动恢复不会绕过 UAC 或 Harness 的检查点校验。关闭工作台窗口不等于暂停成员，停止整个实例会安全保存运行意图。

工作台设置可选随 Windows 登录自动启动（当前用户 HKCU Run，无需 UAC）；空间设置独立选择是否恢复此前运行的成员，默认关闭。关闭整体自启动不停止当前工作台。
