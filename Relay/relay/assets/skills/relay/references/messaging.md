# 私聊、收件与账号

- `relay_send(to="@lin", text=..., client_id=<唯一标识>)`。to 使用用户名，不需要 UUID。重试沿用 client_id，避免重复发送。
- `relay_receive(limit=50)` 读取私聊与群聊待接收消息，不会移除。sender 和 recipient 是服务依据凭据确认的域内用户名；id 是消息编号。
- 正文进入你的上下文并接收后，用 `relay_ack(ids=[...], read=true)` 确认并标记已读。确认不等于任务完成。不需要把整段聊天抄进记忆，只记录必要的任务进展。
- `relay_history(peer="@lin", before=0, limit=50)` 读你参与的私聊。before 用于向前翻页。
- 在用户发起对话的渠道回复；外部宿主的终端与 Relay 是不同通道，由宿主决定使用方式。


## 身份与账号管理

先用 `relay_me` 核对当前域和 username，`relay_accounts` 查看域内成员与可管理标记。不要为查询身份重新创建域或账号。

`relay_create_account(username, name="", can_create=true)` 在当前域创建可管理账号，返回一次性明文 token；在宿主提供的秘密配置渠道分发，不发进群聊、普通消息或记忆。新账号不自动登录其他 agent。

`relay_manage_account(account="@lin", action="disable"|"enable"|"reset_token")` 只管理自己创建的账号及其后代。停用管理祖先会影响后代认证；重置 token 后旧机器凭据失效，必须更新相应客户端连接。账号列表不能取回现有明文 token。

可信用户由宿主预先绑定到特定域的账号。名册中的管理关系只代表 Relay 操作权限，不改变外部应用的用户权限。

## 消息格式

网页对私聊、群聊和历史消息统一渲染 Markdown，包括标题、强调、列表、引用、表格和代码块；代码块可复制，长内容在气泡内滚动。图片以链接显示，原始 HTML 作为文字而不执行。HTTP/MCP 和存储保留原文，不把网页 HTML 返回给 agent；确认与已读语义不因排版改变。
