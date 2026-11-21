# Windows 原生运行（本地预览）

本次适配增加 Windows 后端，Linux 继续使用原有实现。需要原生 Windows Python 3.11+；本轮实际验证为 Windows / Python 3.12。不要使用 WSL Python 创建 Windows 虚拟环境，也不要复用 Linux 的 .venv。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8 = '1'
python -m pip install -e .
```

依赖从各仓库源码安装；私有仓库需要自己的读取身份。pip 不配置跨账户的凭据。各组件运行时不要求源码目录相邻。

使用新的 Windows 数据目录，避免同时打开 WSL 原有数据库和 Thread。Windows 文件锁、UTF-8 文本、进程启动和退出有独立适配；不安装或修改 WSL 服务、Windows 服务、全局代理或防火墙。

## 独立启动

```powershell
python -m relay --root "$env:LOCALAPPDATA\AsterPreview\relay" --port 20920 --mcp-port 20921
python -m unittest discover -s tests -v
```

HTTP、SQLite、私聊/群聊、MCP、浏览器密码和通行密钥复用原业务实现；单实例锁和退出使用 Windows 后端。凭据文件和浏览器认证状态设置受保护 DACL。此项目不依赖 Harness 或 Squad。

前台启动可用 Ctrl+C 正常退出。被宿主管理的进程通过本机命名事件请求正常关闭，不能用 TerminateProcess 冒充安全退出。强制结束进程仍可能中断业务，不提供跨系统恰好一次保证。

手机访问仍需自行设置 HTTPS、公开 URL 和 Windows 网络入口，本次未开放端口或部署网关。不要将 preview 端口与现有 WSL 服务混用。
