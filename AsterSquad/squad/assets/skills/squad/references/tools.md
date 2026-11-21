# 工具与接口

本页描述 Squad 的接口。agent 的 actor 从 MCP Bearer 凭据解析，工具没有 caller/from 参数，最终授权仍由服务端检查；工具名按连接器发现结果调用，不假设所有宿主有相同 namespace。Relay 自己的工具和 HTTP 参见 relay skill。

## Squad MCP（10 个）

| 工具 | 参数 | 用途 |
|---|---|---|
| squad_self | 无 | 自己的有效配置、身份、workspace、模型选项 |
| squad_list | 无 | 域内 agent 名册与 Relay 账号 |
| squad_create | handle, instruction, model="", grants=null, allow_connect=false, gpu_access=false, local_ipc=false, host_systemd=false, sandbox_enabled=true, mcp_services=null | 创建并启动直接 child，自动开户与接入 |
| squad_configure | agent, instruction=null, model=null, grants=null, allow_connect=null, gpu_access=null, local_ipc=null, host_systemd=null, sandbox_enabled=null, mcp_services=null | 编辑直接 child；路径在线应用，模型/身份暂停恢复，保留原 Thread |
| squad_start | agent | 恢复直接 child |
| squad_stop | agent | 正常暂停直接 child |
| squad_reconnect_relay | agent | 重置并重新注入直接 child 聊天凭据 |
| squad_send | to, text | 向 parent / 直接 child 发送管理消息 |
| squad_inbox | limit=50 | 自己参与的管理消息 |
| squad_host_systemd | program, arguments, timeout=30 | 获授权限后在宿主执行 systemctl/journalctl/systemd-run，自动添加 --user；全部用户服务，非逐服务授权 |


## 用户管理与认证

多空间工作台默认 `http://127.0.0.1:19910/squad/`，内部各空间端口由注册表保存，不应猜测。用户可用数据根的 `control/owner.token` 建立初始登录，再配置账号密码和通行密钥；浏览器使用其独立 HttpOnly session Cookie。agent MCP 的 token 不能作为用户管理凭据。

| Fleet HTTP | 用途 |
|---|---|
| GET /api/state | 空间名册及默认空间状态 |
| POST /api/squads | 创建空间：name、description、mode |
| POST /api/domains | 创建独立聊天域 |
| POST /api/resources/{id}/settings | 修改名称和说明 |
| POST /api/resources/{id}/start 或 stop | 起停整个空间 |
| GET /api/squads/{id}/state | 指定空间的成员、实际 instruction、模型、状态和审计 |
| POST /api/squads/{id}/agents | 创建成员，可指定 parent |
| POST /api/squads/{id}/agents/{agent}/configure | instruction/model/grants/gpu_access/local_ipc/host_systemd/sandbox_enabled/mcp_services/run_as_root（root 仅用户，其他权限可在 parent 已有范围内转授） |
| POST /api/squads/{id}/agents/{agent}/start 或 stop | 恢复或正常暂停 |
| POST /api/squads/{id}/agents/{agent}/archive 或 restore | 归档与恢复 |
| POST /api/squads/{id}/agents/{agent}/reconnect | 修复聊天凭据 |
| GET/POST /api/squads/{id}/agents/{agent}/terminal | 查看终端/投递用户输入 |
| POST /api/squads/{id}/bulk | 批量 start/stop，检查每项结果 |
| GET /api/relay-connections | 当前用户自己的聊天连接，可选 selected |

`/squad/api/...` 提供同类工作台路由；旧 `/api/agents/...` 兼容默认空间。单空间服务默认 HTTP 19910、MCP 19911/mcp，用户接口用 `/api/agents/...`，其 owner 凭据与 Fleet 的整体管理入口不可混用。请求体和返回值以当前实现为准。

工作台与 Relay 分别维护网页登录。域账号先存在，连接交接只是登录渠道，不产生新的身份或跨域权限；跨域账号和 cookie 边界由 Relay 独立规则及部署配置决定。

用户在空间设置中选择 `restore_members`（默认关闭），统一控制空间启动后的成员恢复。成员的显式启动、暂停仍通过原工具操作。旧 API 的成员级 `auto_start` 仅作兼容：用户尚未选择空间策略时才有效；已保存的空间策略优先。它不是授权能力，不影响上下级管理范围。
