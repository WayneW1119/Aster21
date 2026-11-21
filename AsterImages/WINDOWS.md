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

## 服务与 MCP

保持原本私有 HTTP worker + 调用者 stdio MCP 两层结构。服务使用 Windows 原生官方 Codex；推荐 `--codex` 指向实际 codex.exe。不要传入 WSL/Linux 可执行文件或复用 Linux Codex home。ChatGPT 登录由宿主在 Windows 单独完成。

```powershell
codex login
# 在宿主环境设置 ASTER_IMAGES_TOKEN（随机、至少 24 字符）后：
aster-images serve --root "$env:LOCALAPPDATA\AsterPreview\images" --port 20931 --codex C:\path\to\codex.exe
python -m unittest discover -s tests -v
```

调用者仍用 `aster-images mcp`，通过 ASTER_IMAGES_URL/TOKEN 接入。参考图和输出路径使用 Windows 路径；根目录与调用者 workspace 边界不变。

Windows 增加独立 Job Object 清理进程树；超时仍不自动重试。`.py` 形式的可执行配置由当前 Python 解释器启动，用于跨平台假后端验收；原 Linux 可执行文件入口保持不变。

自动测试使用假 Codex，覆盖登录限制、原生产物校验、透明要求、超时和 MCP 图片往返。本轮未消耗 ChatGPT 图片额度；Windows 真实内置图片工具与具体 Codex CLI 版本仍需真实验收，不能仅凭假后端通过就宣称端到端生成已验证。
