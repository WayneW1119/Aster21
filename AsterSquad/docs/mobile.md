# 部署与手机访问

Aster Squad 的网页可在桌面及手机浏览器运行。项目只提供产品本身；域名、TLS、路由器、宿主账号集成和系统服务由部署者管理，不要求某个云平台或 Windows/WSL 网络布局。

## 本机访问

启动 `aster-console --root ./data --mode fake` 后访问 `http://127.0.0.1:19910/squad/`，使用 `data/control/owner.token` 登录。默认只监听 loopback。

真实模型使用独立数据目录，并配置模型目录和所引用的环境变量，参见 README。真实模式的 Linux 沙箱依赖必须可用。

## HTTPS 远程访问

准备可从客户端访问的域名和有效证书，反向代理到工作台 HTTP 端口：

```bash
aster-console --root ./real-data --mode real \
  --model-catalog /absolute/path/to/models.json \
  --public-url https://squad.example.com
```

也可在宿主环境设置 `SQUAD_MODEL_CATALOG`、`SQUAD_PUBLIC_URL`。公开 origin 必须明确指定，含非默认端口（如果使用），不能包含路径、查询或凭据。

[Caddy 模板](../deploy/Caddyfile.example) 使用自动 TLS，需替换示例域名并根据自己的网络开放入口。代理保留 `/squad/` 与 `/relay/` 前缀；这两个路径由产品直接支持，不依赖网关重写 HTML。`/squad/` 接收自己的浏览器会话，Relay 代理只转发请求的域账号凭据及 Relay 自己的域浏览器 Cookie，不转发工作台或其他网站 Cookie。

手机可以分别打开 `/squad/` 和 `/relay/` 登录。首次关联可在 Relay 使用一次性的工作台账号交接入口，之后直接用 Relay 自己的密码或 Face ID 登录。公开入口不代理内部 MCP，也不要求对外开放内部随机端口。局域网或蜂窝网络能否访问同一域名，由部署者的 DNS、网络和转发配置决定。电脑运行的宿主需要保持在线。

## 进程守护

[systemd 用户服务模板](../deploy/aster-squad-console.service.example) 假定安装目录为用户自己的 `~/.local/share/aster-squad`，环境文件为 `~/.config/aster-squad/service.env`。它是需调整路径的示例，不会自动安装或修改系统服务。

1. 在安装目录准备虚拟环境，安装 Aster、Relay、Squad。
2. 按 [环境文件示例](../examples/service.env.example) 创建宿主私有配置，权限设为 0600，填写模型路径、公开 origin 和 API 凭据。
3. 复制并调整服务模板到 `~/.config/systemd/user/aster-squad-console.service`。
4. 执行 `systemctl --user daemon-reload` 和 `systemctl --user enable --now aster-squad-console.service`。

模板使用 `KillMode=mixed` 与 `TimeoutStopSec=infinity`，正常停止等待所有成员写完检查点，不按固定超时强杀。只有宿主明确执行强制停止才接受异常中断风险。更新前应正常暂停成员并记录 Thread，备份数据，更新后显式恢复。不要手工篡改 Thread 历史来伪造正常退出。

## 认证

HTTPS 下可设置密码与通行密钥，见 [认证说明](authentication.md)。首次设置需要已有宿主 token，公开访客不能抢注 owner。

本项目不读取其他应用的认证数据库。需要组织 SSO、现有账号或设备网络集成时，在独立的部署项目中实现适配，保留工作台和 Relay 自己的权限边界。

## 验证清单

从浏览器检查 HTTPS、静态资源、登录、创建成员、打开 Relay、发送与接收；匿名管理 API 应返回 401。检查浏览器没有请求服务端的 loopback 地址或 HTTP 混合内容，并确认内部 MCP 不可公网访问。

自动浏览器测试覆盖手机尺寸及 Chromium 虚拟通行密钥，但不能替代真机 Safari、输入法和 Face ID 的实际验证。


## 手机界面与主屏幕

手机工作台默认展示成员列表，可切换分支图；底部导航提供空间选择、聊天域、Relay 和登录设置。点击成员后从底部打开详情，新增或编辑使用全屏表单。成员用户名以小写字母开头，最多 24 位，可含数字、下划线和减号；表单就地解释未填写或不合规的字段，提交期间显示创建进度。

Relay 作为独立页面或应用打开，工作台的快捷入口会另开页面，聊天时不自动跳回工作台。聊天输入随可见视口调整，适配软键盘和屏幕安全区域；触屏回车用于换行，通过发送按钮提交。桌面仍支持 Enter 发送。

iPhone 在 Safari 的分享菜单选择「添加到主屏幕」；Android 在浏览器菜单选择「安装应用」或「添加到主屏幕」。主屏幕图标以独立窗口打开，首次进入仍需登录，可沿用已绑定的通行密钥。工作台与 Relay 各自限定自己的应用导航范围，各自提供独立 manifest 和图标，不依赖私人域名。需要联网使用；service worker 只转发网络请求，不保存账号凭据、API 响应或聊天记录到离线缓存。

`tests/browser_console.py` 覆盖手机新空间的第一个成员创建、字段错误反馈、成员详情开关、触屏多行输入、缩短可见视口后发送和安装资源。`tests/browser_auth.py` 验证底部设置入口及通行密钥流程。
