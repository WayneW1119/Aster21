# Aster Squad

> Windows 原生运行见 [Windows 运行说明](WINDOWS.md)。Windows 11 可启用 MXC 文件沙箱。

基于 [Aster Harness](https://github.com/WayneW1119/AsterHarness) 的常驻 agent 工作台。创建多个 Squad，组织成员分支，管理模型、身份与文件权限，让成员通过独立的 [Relay](https://github.com/WayneW1119/Relay) 与用户和同事交流。

项目提供电脑与手机网页、密码与通行密钥登录、Squad MCP 工具及配套 skills。域名、HTTPS 网关、系统服务和模型凭据由部署者配置，不依赖某台电脑、某个域名或已有的外部登录系统。

## 项目边界

| 项目 | 职责 |
|---|---|
| Aster Harness | agent 的 Thread、模型循环、工具、事件发生器、记忆和文件沙箱 |
| Aster Squad | 成员生命周期、分支、模型 allowlist、权限转授、管理通信与工作台 |
| Relay | 独立的域、账号、私聊、消息队列、HTTP、MCP 和聊天网页 |
| 部署配置 | TLS、域名、反向代理、进程守护与宿主私有凭据 |

Relay 能单独安装使用，不依赖 Aster 或 Squad。Squad 使用已安装的 Relay 包，通过公开 API 管理默认聊天域和账号，不要求两个源码目录相邻。

## 安装

需要 Python 3.11+。Linux/WSL2 的真实模型沙箱要求 bubblewrap 和 libseccomp，未满足时拒绝启动沙箱，不会自动降级。Windows 11 原生运行和 MXC 配置见 [WINDOWS.md](WINDOWS.md)；浏览器可以来自其他操作系统。

```bash
git clone --branch main https://github.com/WayneW1119/AsterSquad.git
cd AsterSquad
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
# 使用经过兼容验证的 Aster SDK 版本，不依赖开发者本机源码。
python -m pip install 'aster-harness[mcp,code-mode,chatgpt] @ git+https://github.com/WayneW1119/AsterHarness.git@main'
python -m pip install 'aster-relay @ git+https://github.com/WayneW1119/Relay.git@main'
python -m pip install -e .
```

Linux 沙箱依赖（Ubuntu/Debian）：

```bash
sudo apt-get install bubblewrap libseccomp2
```

目前仓库为私有，安装前需要配置有仓库读取权的 Git 身份。采用 GitHub 源码安装，不假定这些发行包已上传 PyPI。Aster 兼容版本记录在 [兼容性说明](docs/compatibility.md)。

已经拿到源码目录时，不需要 Git 登录或固定仓库布局。在同一个虚拟环境按实际路径安装即可：

```bash
python -m pip install '/path/to/AsterHarness[mcp,code-mode,chatgpt]' /path/to/Relay /path/to/AsterSquad
```

## 启动与登录

```bash
aster-console --root ~/.aster-squad-demo --mode fake
# 等价：python -m squad.fleet --root ~/.aster-squad-demo --mode fake
```

打开 <http://127.0.0.1:19910/squad/>，从 `~/.aster-squad-demo/control/owner.token` 读取首次登录凭据。工作台可以创建 Squad、建立分支、管理成员和聊天域；点击「打开 Relay」自动交接当前用户的域账号。成员的完整 instruction 可以在详情中查看。

`fake` 是明确标记的离线演示模式，不调用付费模型，不提供真实模型沙箱。它验证 SDK、MCP 与消息监听链路。真实工作请使用 `real` 和独立数据目录，不把已有演示 Thread 转换为真实 Thread。

正常部署默认数据目录为 `~/.aster-squad/`，可用 `ASTER_SQUAD_HOME` 或 `--root` 覆盖。运行数据独立于源码；三层 skills、workspace 归属和旧部署迁移见 [数据与 skills](docs/data-and-skills.md)。

## 使用真实模型

复制并修改 [examples/models.json](examples/models.json)，按服务商提供的实际模型名称和 Responses API 地址填写。示例支持每位成员选择不同的服务，目录只引用环境变量，不保存 API Key：

```bash
export DEEPSEEK_API_KEY='your-api-key'
export OPENAI_API_KEY='your-api-key'
aster-console --root ~/.aster-squad --mode real --model-catalog ./examples/models.json
```

模型密钥只配置给宿主，由宿主选择性传给对应 worker；不传给网页或 instruction。也可使用 Aster 的 ChatGPT 登录配置，详见 [模型配置](docs/console.md#在同一个-squad-使用不同模型服务)。

## 手机、密码与 Face ID

网页自带手机布局。远程访问需要部署者提供 HTTPS 反向代理并配置 `--public-url https://squad.example.com` 或 `SQUAD_PUBLIC_URL`。只代理 `/squad/` 与 `/relay/`，保留原路径，内部 HTTP/MCP 继续只监听 loopback。

HTTPS 下，首次用管理凭据登录后，在「登录与安全」设置自己的账号密码，再在 iPhone Safari 绑定通行密钥。以后可以使用密码、Face ID、指纹或设备解锁；具体方式由系统决定。不要把本地 token 入口直接当作已经配置好公网部署。

- [部署与手机访问](docs/mobile.md)，附通用 Caddy 和 systemd 模板。
- [账号、密码与通行密钥](docs/authentication.md)。

## 恢复与边界

正常暂停和修改配置保留同一个 Aster Thread。工作台重启会恢复启用的 Squad 服务。工作台设置可选择随系统启动；每个空间可选「空间启动后恢复成员」，默认关闭，开启后只恢复仍请求运行且没有错误的成员。主动暂停、归档及报错的成员不会自动拉起。修改开关立即保存，不重启当前 worker。异常 Thread 仍须通过 Harness 的恢复校验，不会自动重试或替换 Thread。异常恢复遵循所安装的 Aster 能力，失败不会偷偷创建替代 Thread。

当前每个 Squad 最多 16 位成员，工作台按需创建 Squad，不再设固定的 8 个空间上限；空间设置支持删除登记并保留本机数据。默认 Relay 已支持私聊和群聊；暂未提供附件、通用模块市场、费用预算或任意模型故障自动修复。用户可独立开关成员沙箱，关闭时保存路径规则，再开启时恢复。用户可在成员编辑页单独授予完整的宿主用户级 systemd 管理权；该能力可执行宿主代码，超出路径沙箱，不自动传给下级。文件权限不是网络隔离，未匹配路径规则时禁止访问；现有成员的全局可读由可编辑的 `/=read` 规则表达；详见 [实现与边界](docs/implementation.md)。

## 开发和验证

```bash
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m playwright install chromium
python tests/browser_auth.py
ASTER_BROWSER_PREFIX=/squad python tests/browser_console.py
```

测试使用临时数据、脚本模型与真实 SDK/MCP，不需要模型密钥。Linux 沙箱测试需要可用的 user namespace、bubblewrap 和 libseccomp。截图、环境与数据不进入仓库。

- [工作台与分支管理](docs/console.md)
- [实现与消息生命周期](docs/implementation.md)
- [MCP 工具与接口](docs/tools.md)
- [额外 MCP 服务配置](docs/console.md#额外-mcp-服务)：宿主注册服务，工作台按成员启用；网页检索可连接独立 [Aster Web](https://github.com/WayneW1119/AsterWeb)
- [instruction 示例](docs/instruction-example.md)
- [instruction 模板](squad/assets/instruction.md) · [统一 Squad skill](squad/assets/skills/squad/SKILL.md)（架构、使用、权限、接口与排错）

当前仓库暂未指定许可证。

GitHub Actions 使用固定提交的 Harness、Relay 源码。私有依赖分别使用只读 deploy key：Harness 仓库的公钥对应本仓库 secret `ASTER_HARNESS_READ_KEY`，Relay 对应 `RELAY_READ_KEY`；两把密钥不能写仓库，也不使用个人管理 token。迁移仓库或轮换密钥时需同步设置；不授予来自 fork 的 PR 这些凭据。沙箱检查仅在临时 CI runner 上配置 user namespace 权限，不改变部署机器设置。

项目依赖、平台能力和私人部署配置的边界见[项目边界与部署约定](docs/project-boundaries.md)。
