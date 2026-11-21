---
name: squad
description: 理解、使用、集成或排查 Aster Squad：空间和成员架构、parent/child、生命周期、模型与路径授权、管理通信、三层 skills、工作台与 MCP。
---

# Aster Squad

Squad 是常驻 agent 的管理空间：保存成员身份和分支，配置模型、instruction 与路径授权，启动或暂停成员，并提供管理通信。Harness 运行各成员的持久 Thread；默认聊天模块通过独立 Relay 服务接入。

先按需要用 `tool_search` 发现 Squad 工具。`squad_self` 查询自己的有效配置和模型选项，`squad_list` 查询名册；不要猜成员 ID 或权限。通过 MCP 只能管理自己的直接 child，用户工作台具有整体管理入口。

## 按问题选择参考

用 `skills.read(skill_id="squad", resource="references/文件名")` 按需读取，先选择最相关的一份。

| 问题 | 参考 |
|---|---|
| 空间、成员、Thread、进程、消息和模块之间的关系 | [架构与机制](references/architecture.md) |
| 创建和管理 child、委派、与用户沟通、恢复工作 | [日常使用](references/usage.md) |
| 沙箱开关、路径权限、宿主 systemd 授权、转授、用户身份和管理边界 | [权限与身份](references/permissions.md) |
| 10 个管理 MCP 工具、用户 HTTP 与工作台入口 | [接口](references/tools.md) |
| Relay 通知如何接入 Aster 程序事件 | [聊天监听接入](references/relay-integration.md) |
| home、workspace、全局/空间/个人 skills 和迁移 | [数据与 skills](references/data-and-skills.md) |
| 开发集成、排错、验证和实现限制 | [开发与排错](references/development.md) |

## 使用原则

workspace 由成员自己支配；正式项目放在外部并显式授权。创建者给出的身份是起点，经历、培训和工作进展由成员通过原生记忆机制维护。

仅改 grants 或沙箱开关时，在步骤边界在线应用，不重启 worker；仅改额外 MCP 选择且无需新增凭据时也在线应用，须检查回执中的不可用服务；修改模型或 instruction 时，系统正常暂停后恢复同一个 Thread；失败明确报告，不偷偷新建替代 Thread。短 instruction 保存基础身份和连接信息，详细机制放在本 skill 和参考中。

日常聊天、群聊与消息确认由 `relay` skill 说明；Squad 的事件监听适配见上面的聊天监听接入；Harness 的原生运行机制由 `aster-harness` 说明。这里只有接入关系，不另定义一套聊天或运行时语义。文档对应已发布实现，具体参数和能力以当前工具 schema、配置与返回结果为准。

## 成员套件

新成员的 Squad 基础套件提供成员管理连接、默认 Relay 账号连接和收件监听，以及 Squad、Relay 和 Harness 的指南。监听通知里的 `payload.listen_name` 标识收件账号。当前发生器列表中带 `squad/inbox` 的记录由宿主管理，恢复后检查一次即可。旧成员沿用原有连接和监听。

工作台的能力套件区按宿主目录展示可选服务。Web 提供搜索和网页读取，Images 用本地 stdio 客户端访问图片服务，Browser 用 Playwright stdio 操作浏览器。开启相应套件后查阅其 skill 和当前工具列表。权限区单独配置路径、设备、本机进程通信和宿主服务管理。开启套件时仍须满足实际运行权限。
