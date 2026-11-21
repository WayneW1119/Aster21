# Windows 文件沙箱

Windows 11 使用 Microsoft MXC 的原生 BaseContainer。宿主显式配置已签名的 `wxc-exec.exe`。执行前探测当前请求的路径策略和后端能力。探测失败时拒绝启动。Linux 继续使用其原有文件沙箱。

## 路径与网络

- 未匹配路径默认禁止。`/` 是跨本地盘符的根规则；显式 `/=read` 开放全局读取，删除后恢复默认禁止，也可显式设为 write 或 deny。普通盘符/更深路径覆盖跨盘符根规则。工作区、显式路径和必要的运行时路径共同决定有效权限。宿主保护目录内的授权会被剔除。
- 禁止规则同样应用于命令、文件读取、目录列表、补丁、自定义脚本、stdio MCP 和事件程序。在线修改权限时，旧命令结束，MCP 和事件程序用新策略重启。Thread 继续使用原有记录。
- 默认允许联网，包括本机回环。stdio MCP 的解释器和脚本必须位于可读范围。固定的运行时只读例外不开放用户目录。
- **已有硬链接**可以让可读工作区内的另一个路径指向工作区外的同一文件。对原路径的拒绝无法阻止从已有硬链接读取。使用可信工作区，避免把敏感文件的硬链接放在可读范围内。

## 验证

工作台的 `sandbox-mxc-probe` 检查原生后端、拒绝路径和回环支持。`sandbox-mxc-smoke` 在临时工作区实际执行读写、拒绝和联网检查。测试所需的文件和回环服务均为临时资源。

Harness 测试入口为 `python scripts/test_windows.py`。设置 `ASTER_MXC_TEST_EXECUTOR` 与 `ASTER_MXC_TEST_PYTHON` 以运行原生隔离测试，可用 `ASTER_MXC_TEST_POWERSHELL` 指定测试 shell。

[Microsoft MXC 的网络说明](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/networking.md)

命令进程先以挂起状态创建，加入 kill-on-close Job 后再恢复执行；取消或启动失败会回收已创建的进程。命令不设隐式五分钟生命周期，由 Harness 的命令句柄、停止和权限更新机制管理。当前不支持 ConPTY、Linux systemd 或 WSL GPU 设备授权；管理员模式仍仅用户可设置。
