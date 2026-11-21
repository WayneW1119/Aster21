# Relay

> 新增 [Windows 原生运行说明](WINDOWS.md)。

独立域聊天工具，供人、agent 和程序使用。**不依赖 Squad，也不依赖 Aster 运行时。**

一个进程服务一个域，包含网页私聊与群聊、账号管理、HTTP API 和 14 个 MCP 工具。所有人使用自己的账号，聊天没有上下级限制。管理者可以创建账号，管理自己创建的账号及其下属账号，创建时可关闭该账号继续创建账号的权限。

## 安装与启动

需要 Python 3.11+。Relay 的 HTTP、MCP 与网页不需要安装 Aster 或 Squad，也不要求其他仓库放在相邻目录。

```bash
git clone https://github.com/WayneW1119/Relay.git
cd Relay
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m relay --root ~/.relay-demo --name '我的工作室'
```

网页 <http://127.0.0.1:19920>，MCP <http://127.0.0.1:19921/mcp>。首次启动创建 user 账号，登录凭据保存在 `~/.relay-demo/owner.token`，粘贴到网页即可。
端口可用 `--port`、`--mcp-port` 修改。正常暂停服务保留域、账号和聊天历史；同一个数据目录不能同时运行两个服务。

## 网页功能

- 查看和搜索域成员，私聊，Enter 发送、Shift+Enter 换行。
- 消息持久保存，离线成员上线后可读取；按会话显示未读数。
- 打开会话将已加载入站消息标记已读，发送者看到已读状态。
- 查看更早的聊天记录；消息支持 Markdown 标题、强调、列表、引用、链接、表格和代码块；原始 HTML 按文本显示。
- 创建账号、设置显示名称和开户权限，复制新凭据；停用、启用、重置受管理账号的凭据。
- 重置后旧凭据立即失效；停用管理者时，其下属账号也无法认证。
- 手机尺寸可在联系人列表与私聊之间切换。
- 域快捷栏和域选择器，可保存并切换多个独立服务的账号连接。
- 手动连接已有域，或由 Squad 工作台同步当前用户的域连接；移除连接不删除服务或聊天记录。

支持群聊、邀请/移除、个人禁言与全员禁言；每群最多 100 人。暂不支持附件、跨域聊天、公开注册和密码找回。用户凭据不是模型 API Key；新建和重置时会提供一次明文凭据，账号列表无法取回。

域切换不是跨域投递：每条消息仍只存在其所属域。浏览器切换时取消旧请求并清空会话视图，使用目标域自己的凭据认证。机器凭据不能跨域使用，相同用户名在不同域仍是不同账号；本机不同端口通过受限的本地 CORS 来源规则访问。没有 Squad 的情况下，选择器中的「连接已有域」也可独立使用。

用户名（username）在域内唯一，大小写不敏感并统一保存为小写，创建后不可修改或复用。显示名称（name）可以重复。内部 ID 仅用于存储关联，agent 不需要复制它。

账号先由管理员创建或分配，再把凭据配置给 agent。MCP 连接使用凭据认证；relay_me 只是登录后的身份查询。

## MCP 工具与文档

完整 [relay skill](relay/assets/skills/relay/SKILL.md) 按任务路由到独立的架构、消息、群聊、监听、接口和排错参考。14 个 MCP 工具及 HTTP 接口统一维护在 [接口参考](relay/assets/skills/relay/references/interfaces.md)，不在此重复一份可能过期的表格。

MCP 以账号 Bearer token 认证；HTTP 使用账号 token 或浏览器自己的 HttpOnly session Cookie。网页可以设置密码和通行密钥，各域分别登录；机器 token 与浏览器会话独立。通过 HTTPS 和正确 origin 使用设备 Face ID/指纹，服务并不接收生物特征。

## Agent 接入

为 agent 配置已存在域的 MCP 地址、自己的唯一用户名和账号凭据，挂载 [relay skill](relay/assets/skills/relay/SKILL.md)。可信用户绑定由宿主 instruction 提供，Relay 不自行宣布某个账号拥有用户权威。

Relay 只定义通用 HTTP/MCP 协议。Aster Squad 的通知适配脚本由 Squad 提供，其他宿主可以通过长轮询自行接入事件机制；Relay 本身不导入或安装任何 agent 运行时。

连接文件：

```json
{"url":"http://127.0.0.1:19920","username":"lin","token":"自己的凭据"}
```

监听接口 `GET /api/wait?after=0&timeout=25` 返回 `{latest,pending}`。不使用 Aster 的程序也能使用这个 HTTP 接口自行监听，不需要安装 Aster。

## 消息语义

发送落库后即返回，不等对方回复。receive 不会弹出或删除消息；ack 让它退出待接收队列；read 同时设置聊天已读。历史保留。
通知可以重复，消息 ID 稳定；带相同 client_id 的同一发件人重复发送相同消息只生成一条，复用编号发送不同内容则报冲突。不保证任意崩溃下业务处理恰好一次。

## 部署边界

域名、TLS、系统服务和私有账号凭据由部署者管理，不属于 Relay 的特定宿主配置。默认监听 loopback；不要把未经验证的端口转发当作公网部署。

[Aster Squad](https://github.com/WayneW1119/AsterSquad) 可以为用户代理多个 Relay 域的浏览器接口；Relay 的独立服务仍通过每个账号自己的凭据认证。独立远程部署需要自行提供 HTTPS 反向代理并检查前缀、浏览器来源和认证配置。当前 CORS 规则只支持本地来源，未提供任意跨站远程域白名单。

从 Relay 0.4 起，原 Aster 专用 watch.py 由 Squad 持有。升级旧集成时同时升级 Squad，已有成员连接目录和发生器入口不改名，也不批量改写私人脚本。HTTP/MCP 客户端不受这项归属调整影响。

## 实现与验证

```text
relay/store.py      SQLite 正本、凭据哈希、账号管理与消息权限
relay/server.py     HTTP、长轮询、网页、进程生命周期
relay/mcp.py        14 个认证 MCP 工具
relay/web/          完整网页版
relay/assets/skills/relay/   通用协议与使用 skill
tests/              不依赖 Squad / Aster 的 HTTP 和持久化测试
```

```bash
python -m unittest discover -s tests -v
```

Squad 项目还包含独立启动 Relay 的双浏览器验收。服务日志不记录认证头；聊天、工具结果等业务内容仍属于需要正常管理的应用数据。

当前仓库暂未指定许可证。


### 手机与主屏幕

手机界面适配屏幕安全区域和软键盘，触屏回车用于换行，点击发送按钮提交。Safari 可通过「分享 → 添加到主屏幕」以独立窗口打开；Android 可使用浏览器的安装入口。manifest、图标和网络转发 service worker 都由 Relay 自己提供，独立运行时也可使用。使用需要联网，service worker 不缓存登录凭据、API 响应或聊天记录。可直接作为独立应用打开，首次按需关联宿主已有聊天账号。


## 独立网页登录与 Face ID

Relay 自己提供域账号的密码与通行密钥登录，不依赖 Squad、Aster 或第三方登录服务。已有域账号可以先用管理员分配的凭据连接，在「登录与安全」设置密码，再绑定手机通行密钥。以后选择自己的域，直接用用户名密码、Face ID 或其他系统通行密钥登录。账号停用后，网页会话与新登录都立即失效；退出 Relay 不影响其他应用。

使用 HTTPS 反向代理时配置固定外部地址（含路径）：

```bash
relay --root ~/.relay-demo --public-url https://chat.example.com
# 本机通行密钥开发可使用 localhost：
relay --root ~/.relay-demo --public-url http://localhost:19920
```

反向代理需保留 Relay 自己的 Cookie、Origin 和认证响应的 Set-Cookie。不要把任意转发头当作外部 origin。HTTPS 会话 Cookie 使用 Secure、HttpOnly、SameSite=Strict，并限定外部 URL 路径；只有本机 HTTP 调试使用普通 Cookie 名。密码使用加盐 scrypt；登录会话 ID 和 challenge Cookie 只保存摘要，通行密钥保存公钥。认证接口为 `/api/auth/`，与 MCP 的机器凭据独立。更改密码或移除密钥会撤销当前账号的旧浏览器会话。

浏览器可持久保存域地址和用户名提示，API 账号凭据不写入长期 localStorage。首次集成宿主可由用户主动交接一次已有聊天账号，之后网页登录不再依赖宿主会话。通行密钥验证固定 origin、RP ID、签名、用户句柄及设备验证；challenge 三分钟过期且不可重用。

## 群聊

网页左侧「我的群聊」→「建群」，填写群名并选择本域账号。群内顶部「群设置」支持邀请、移除、改名、单人禁言、全员禁言、退群和解散。手机同样可用。第一版只分群主和成员：群主负责管理，全员禁言时群主仍可发言。群主退出须解散，不含管理员、多群主和群主转让。群名称可重复，群编号唯一；每群最多 100 人。

任何启用的账号均可建群，不受账号 can_create（开户权限）限制。仅当前群成员能访问群；新成员和重新入群成员只能查看此次入群之后的消息。退群、被移出或解散后不能继续读取历史或待收群消息。群消息投递给除发送者外的当前成员，每个成员独立确认，重试不会重复投递；私聊不混入群消息。账号禁用会阻止登录和群操作，不会自动转移群主。

REST：GET/POST `/api/groups`，GET/POST `/api/groups/{id}`，GET/POST `/api/groups/{id}/messages`。管理 POST 的 action 支持 invite（members 用户名列表）、remove/mute/unmute（member 用户名）、rename（name）、mute_all/unmute_all、leave、dissolve。发送群消息沿用 text/client_id；历史沿用 before/limit。群历史 id 是群消息编号，delivery_id 是当前账号的队列编号，确认使用后者。

MCP 新增 `relay_groups`、`relay_group`、`relay_create_group`、`relay_manage_group`、`relay_send_group`、`relay_group_history`。`relay_receive` 同时接收私聊与群聊，群消息带 group_id；`relay_ack` 和已有监听脚本保持兼容。群历史 MCP 返回的 message_id 只用于分页，确认使用 receive 返回的 id。详见 [Relay skill](relay/assets/skills/relay/SKILL.md)。

旧域启动时自动增加群表及投递字段，不重建账号、token、私聊或浏览器认证数据。升级前先备份 SQLite 数据库；降级旧版本不能保证理解群投递，请勿直接在已经产生群消息的数据库上运行旧版服务。

独立部署默认数据目录为 `~/.relay/`，可通过 `RELAY_HOME` 或 `--root` 指定；不会自动迁移旧 `./data`。由 Squad 托管时，使用 Squad 显式分配的数据目录。

## Markdown 消息

私聊、群聊与历史消息使用同一渲染器，已有消息刷新后即可按 Markdown 显示。代码块提供复制按钮，长代码和表格在气泡内滚动。外部链接在新标签打开，图片显示为链接而不自动请求第三方资源。数据库、HTTP 和 MCP 始终保留消息原文。

网页随包内置 markdown-it 15.0.3，关闭原始 HTML，限制链接协议；不依赖运行时 CDN。第三方许可证和来源记录随 web 资源分发。修改网页逻辑请编辑 `relay/web/app-main.js` 或 `relay/web/markdown.js`，运行 `python tools/build_web.py` 更新已提交的自包含 `app.js`；安装使用者不需要 Node 或构建步骤。

### 文件传输

聊天输入区点击 **＋** 选文件，再点击发送。私聊和群聊均支持，可附带文字；
消息卡片显示文件名、大小及下载按钮。单文件上限 **25 MiB**，上传和服务端下载采用流式处理。
下载需要原域账号登录，遵循私聊双方、群成员与入群时间的访问范围，群禁言也限制发送文件。
选中的图片会显示发送前预览；聊天中的 PNG、JPEG、GIF、WebP 可显示缩略图并点击查看大图。
预览通过账号认证读取，按可见区域加载，浏览器缓存有上限；下载始终保留原文件，其他格式仍显示文件卡片。
HTTP 接口和备份范围见
[接口说明](relay/assets/skills/relay/references/interfaces.md#文件消息)。
