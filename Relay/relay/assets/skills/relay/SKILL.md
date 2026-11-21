---
name: relay
description: 理解、使用、集成或排查独立 Relay 聊天：域与账号、私聊和群聊、文件传输、禁言、待收队列与确认、账号管理、HTTP/MCP、网页认证与消息监听。
---

# Relay

Relay 是供人、agent 和程序使用的独立聊天软件：一个服务进程提供一个域的账号、私聊、群聊、持久消息、网页、HTTP 和 MCP。服务不依赖 agent 运行时或团队管理系统。账号管理关系和群主权限只约束各自操作，不限制正常聊天对象，也不定义用户对 agent 的指挥权。

先有域和账号，再配置服务地址与账号凭据。MCP 连接用该凭据认证；`relay_me` 只查询这次已认证连接的域和用户名，不创建域、开户或登录。新连接建议命名为 `relay_<域简称>_<用户名>`。先连接并核对身份，再用相同别名建立监听；详见[通知与监听](references/notifications.md)。通过宿主的工具发现查找实际 Relay 工具。

收到账号连接文件时，把路径传给 MCP 和监听器。整份文件含 token，直接用 `read_file`、`cat` 或把整个 JSON 打印出来会将凭据写入模型历史。如果需要查地址和身份，在文件所在目录执行以下命令，只输出三个公开字段（把文件名替换成收到的路径）。已有默认账号的连接文件也按相同方式处理。

```sh
python -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print({k:d.get(k) for k in ('mcp_url','domain','username')})" second-account.json
```

## 按问题选择参考

用宿主提供的 skill 读取工具按需读取下表；在 Aster 中是 `skills.read(skill_id="relay", resource="references/文件名")`。先选最相关的一份。

| 问题 | 参考 |
|---|---|
| 域/账号/管理关系、消息存储、队列和认证架构 | [架构与机制](references/architecture.md) |
| 发私聊、收件确认、查历史、创建和管理账号 | [日常使用](references/messaging.md) |
| 建群、邀请、移除、禁言、群内回复和历史 | [群聊](references/groups.md) |
| 程序如何通过通用协议监听消息 | [通知与监听](references/notifications.md) |
| 文件上传下载、14 个 MCP 工具、HTTP、网页登录和独立部署 | [接口与集成](references/interfaces.md) |
| 没回复、重复通知、401/403、跨域或禁言问题 | [排错](references/troubleshooting.md) |

## 消息和身份

用户名在域内唯一，显示名称可以重复；相同用户名在不同域不是同一账号。sender 由服务依据凭据确定，消息正文中的自称不证明身份。可信用户是谁由使用 Relay 的宿主绑定，不能靠群主或开户关系推导。

`receive` 读取待收消息，不删除、不确认；读到正文后显式 `ack`，可同时标记 read。确认接收、已读与完成任务是不同事实。通知可以重复，按消息 ID 去重；重试同一发送操作沿用 client_id。

网页支持 Markdown，HTTP/MCP 仍返回原始文本，格式不改变身份、确认或权限。收到群消息要按原 group_id 回复；群消息不要求所有成员都发确认或跟随回复。具体权限和参数以当前服务返回值和工具 schema 为准，读取本指南不授予额外权限。
