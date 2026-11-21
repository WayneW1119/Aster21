# Aster Suite

Aster 系列项目的当前源码快照，供直接下载和本地安装。这个仓库只有一次快照提交，不包含原项目的 Git 历史，也不包含运行数据、聊天记录、记忆、账号凭据或私人部署配置。

## 下载

[下载完整源码 ZIP](https://github.com/WayneW1119/Aster21/archive/refs/heads/main.zip)。仓库公开期间无需登录 GitHub。

## 包含的项目

| 目录 | 用途 | 快照版本 |
| --- | --- | --- |
| `AsterHarness/` | Agent 运行时、Thread、工具、事件、记忆与文件沙箱 | 0.1.28 |
| `AsterSquad/` | Squad 工作台、成员与权限管理 | 0.4.27 |
| `Relay/` | 独立聊天服务、域、账号、群聊与 MCP | 0.5.2 |
| `AsterWeb/` | 可选网页检索和抓取 MCP，接入 Exa、DeepSeek | 0.1.0 |
| `AsterImages/` | 可选图片生成和编辑 MCP | 0.1.1 |

五个项目均取自 main。准确的来源提交见 [sources.json](sources.json)。原项目链接可能需要私有仓库权限；使用本快照不需要访问那些仓库。

## 安装

支持 Linux、WSL 和原生 Windows，使用 Python 3.11 或更新版本。Windows 安装及沙箱说明见 [WINDOWS.md](AsterSquad/WINDOWS.md)。在 Windows PowerShell 中使用下列安装命令时，将激活步骤换为 `.\.venv\Scripts\Activate.ps1`。

Ubuntu / Debian 上，真实 agent 的文件沙箱需要：

```bash
sudo apt-get install bubblewrap libseccomp2 python3-venv
```

解压 ZIP，进入包含本文件和五个项目目录的文件夹，执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install './AsterHarness[mcp,code-mode,chatgpt]' ./Relay ./AsterSquad ./AsterWeb ./AsterImages
```

这条命令使用包内的五份源码，不需要 GitHub 登录。第三方 Python 依赖仍需联网下载。只需要核心功能时，可以省略最后的 `./AsterWeb ./AsterImages`。

## 开始使用

可以先启动不会调用真实模型的演示工作台：

```bash
aster-console --mode fake
```

打开 <http://127.0.0.1:19910/squad/>，首次管理凭据默认位于 `~/.aster-squad/control/owner.token`。在工作台中设置自己的登录账号、密码及通行密钥。

真实模型与正式部署请阅读各项目说明：

- [Aster Harness](AsterHarness/README.md)：运行时、SDK 与模型认证。
- [Aster Squad](AsterSquad/README.md)：工作台、模型配置和权限管理。
- [Relay](Relay/README.md)：独立聊天服务。
- [Aster Web](AsterWeb/README.md)：搜索服务与供应商配置。
- [Aster Images](AsterImages/README.md)：图片工具及 Codex 配置。

使用本快照安装时，以本页的本地源码安装命令为准；子项目中从私有 GitHub 仓库安装的步骤可跳过。新机器需要配置自己的模型登录或 API Key、HTTPS 和端口访问。图片工具另需官方 Codex CLI 与相应登录。

未新增项目许可证；子项目已有的第三方许可证和声明保持原样。
