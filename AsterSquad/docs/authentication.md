# 自设密码与 Face ID

先按 [部署说明](mobile.md) 为自己的工作台配置 HTTPS。以下操作在自己的 `/squad/` 网页完成。

## 首次设置

1. 用现有管理凭据登录，点击右上角「登录与安全」。
2. 填写自己的账号和密码，确认后保存。密码至少 12 个字符。请自己在网页输入，不要通过聊天发送。
3. 在 iPhone Safari 打开同一网址，用刚设置的账号密码登录。
4. 再次打开「登录与安全」，给密钥起名，例如「我的 iPhone」，点击「绑定此设备的通行密钥」，按 iOS 提示保存并完成 Face ID。
5. 以后需要登录时，选择「使用通行密钥 · Face ID / 指纹」。电脑也可使用密码、系统通行密钥，或支持时用手机扫码完成验证。

网站请求的是设备用户验证；具体使用 Face ID、Touch ID、设备密码还是其他认证器，由设备及系统决定。网站不接收生物特征。iPhone 使用通行密钥的说明见 [Apple 文档](https://support.apple.com/en-mide/guide/iphone/iph37306ae67/ios)。

密码仍可作为恢复方式。管理 token 保留为宿主管理和恢复凭据。修改密码需要当前密码（使用宿主 token 恢复时除外），并撤销之前的密码/通行密钥网页登录会话。通行密钥可在此页面移除，移除也会撤销已有此类会话；当前浏览器获得新会话。宿主 token 是独立的管理和恢复凭据，不因修改网页密码而撤销。

网页登录会话有效 30 天，退出登录立即撤销当前会话。关闭浏览器不会暂停 agent。会话仍有效时无需再次验证 Face ID。

## 实现

- `squad/browser_auth.py`：单一 owner、scrypt 加盐密码哈希、SQLite 会话、限流、WebAuthn 签名验证。
- 私有数据库：`data/control/fleet/browser-auth.sqlite3`，文件权限 0600，不保存明文密码或浏览器会话原文，只保存会话摘要；只保存通行密钥公钥。
- 会话 Cookie：`__Secure-aster_squad_session`，Secure、HttpOnly、SameSite=Strict，限定 `/squad/` 路径。
- 通行密钥 challenge 有效期三分钟，只使用一次，注册绑定当前登录会话。服务端校验固定 origin（含端口）、RP ID（域名）、签名、计数器及用户验证标志。近期登录十分钟内可绑定/移除；超过后需提供当前密码，或使用宿主 token。
- 所有认证 POST 要求精确同源 Origin；Squad Cookie 不能认证内部旧 `/api/` 或 Relay API。Relay 使用自己独立的域账号和浏览器会话。
- 认证 POST 不记录密码，相关响应不缓存，页面禁止嵌入第三方 frame。密码登录全局限流 20 次/五分钟，通行密钥也有独立限流。
- 初次设置必须已有宿主 token 或已经认证的 owner 会话。公开访问不能抢先注册账号。

新增接口均在 `/squad/auth/`：GET `status`；POST `password/login`、`password/set`、`logout`、`passkeys/register/options`、`passkeys/register/verify`、`passkeys/login/options`、`passkeys/login/verify`、`passkeys/remove`。它们仅用于工作台 owner，不是 agent MCP 工具。

WebAuthn 由 `webauthn>=2.8,<3` 实现，应安装在本项目的虚拟环境中。

## 验证与边界

- Squad 测试覆盖首设授权、密码校验、改密撤销、退出、持久化、限流、Origin、challenge 重用拒绝及与 Relay 凭据的隔离。
- `tests/browser_auth.py` 使用临时独立工作台及 Chromium 虚拟认证器产生真实签名，完成注册、通行密钥登录、密码登录/修改、密钥移除，拒绝篡改签名和重放；测试凭据不写入正式数据库。
- 原有浏览器编组、分支、域切换和聊天流程通过回归。
- 真实 iPhone Face ID 的注册必须由用户在手机上完成；尚未声称经过真机生物识别测试。


## 独立的 Relay 登录

Squad 工作台与 Relay 是两个可同时打开、分别添加到主屏幕的应用，密码、通行密钥和退出状态互相独立。

1. 单独打开 `/relay/`，选择聊天域。已经有 Relay 密码或通行密钥时，可直接登录。
2. 首次连接已有账号，可在 Relay 点击「首次连接：使用已登录工作台的聊天账号」。此操作只在明确点击时使用当前工作台会话交接一次聊天账号，页面不跳转；也可使用域管理员分配的账号凭据。Relay 独立部署仍可直接使用自己的账号凭据。
3. 在 Relay 的「登录与安全」设置当前域账号自己的密码，再在手机上绑定通行密钥。无需修改或重绑已有 Squad 通行密钥。
4. 以后可直接打开 Relay，用该域用户名和密码或 Face ID 登录。浏览器只持久保存无秘密的域地址和账号提示；登录会话通过 Relay 自己的 HttpOnly Cookie 保持。退出任一应用不会退出另一个。

Relay 浏览器认证属于 Relay 项目，工作台只代理允许的认证接口和 Relay 专属 Cookie；认证 Cookie 按 `/relay/domains/{id}/` 隔离，Squad 和其他网站 Cookie 不传给聊天服务。不同域的浏览器账号仍各自独立。密码或通行密钥变更只撤销当前账号的旧网页登录会话；MCP 继续使用原有机器凭据。

`tests/browser_independent_auth.py` 覆盖两个手机页面各自注册/登录真实签名的虚拟通行密钥、一次性交接、互不影响的退出、新标签页恢复、Relay 收发消息、错误签名和重放拒绝。真机 Face ID 绑定必须由用户在自己的设备上完成。
