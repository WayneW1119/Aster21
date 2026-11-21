# Squad 工具与接口

完整接口说明由随安装包发布的 [squad 参考](../squad/assets/skills/squad/references/tools.md) 维护，包括 9 个 MCP 管理工具、用户 HTTP、工作台和认证边界。

Relay 自己的消息、群聊和账号接口见独立 Relay 项目的 relay skill。

`squad_create` 与 `squad_configure` 支持可选布尔值 `auto_start`（创建默认 false，配置省略则保留）。控制空间重启后的恢复，不立即启动/暂停，也不扩大管理权限。正常启动记录运行意图，正常暂停清除；启动恢复只尝试一次，已有错误或未通过 Thread 恢复校验时留给用户处理。
