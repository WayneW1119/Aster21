# Windows 原生运行

Aster Squad 支持在 Windows 上运行工作台、Relay 和成员进程。Windows 11 可配置微软 MXC 作为成员文件沙箱。Linux 和 WSL 使用原有的 Linux 启动方式及权限后端。

需要原生 Windows Python 3.11 或更新版本。请在同一个 Windows 虚拟环境中安装支持 Windows 的 AsterHarness、Relay 和 AsterSquad。AsterWeb 与 AsterImages 为可选组件，原生窗口需要 AsterSquad 的 `windows-window` extra 和 Microsoft Edge WebView2 Runtime。详情见 [安装和启动](scripts/windows/README.md)。

Windows 工作台使用独立数据目录。Linux 和 Windows 的安装可同时运行，各自保管数据库、Thread 和凭据。
