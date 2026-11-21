# Relay 与 Aster 事件接入

Squad 负责把独立 Relay 的 HTTP 通知转换为 Aster 程序事件。宿主把 squad/assets/relay_watch.py 复制到成员连接目录的 watch.py；instruction 提供脚本和连接文件路径。通知带用户名、积压数和可选的监听名，不带消息正文或凭据，也不替 agent 确认消息。

使用基础套件的成员由宿主注册默认监听。收到带 `payload.listen_name` 的提示时，通过该别名对应的 MCP 连接读取并处理原消息。额外账号建议先连接 MCP，调用该连接的 `relay_me` 核对域与用户名，然后用该账号的连接文件和 `--listen-name 连接名` 注册独立监听。监听名由 Relay 使用者协调保持可区分。

## 自建监听与旧成员

连接文件和脚本位置由宿主提供。它们不必须位于 workspace；不要猜 `.relay/watch.py` 或 `.connections/relay.json`。在 Aster 中使用宿主提供的实际 `WATCH` 和 `CONNECTION` 路径：

1. `list_event_generators` 检查已有活跃监听，正常恢复通常会重启原来源。
2. 查看实际脚本，再用 `test_program_generator(script_path=WATCH, args=["--connection", CONNECTION, "--self-test"], mode="once")` 做有限自测；核对连接结果。
3. 没有同用途活跃来源时，`register_program_generator(script_path=WATCH, args=["--connection", CONNECTION], mode="service")` 注册，随后检查状态和日志。
4. 提示到来后读取原始队列，处理并确认；无事时用运行时等待工具，不空转模型。

脚本批次通知间隔至少 5 秒，未确认积压约每 60 秒提醒；它不保证恰好一次通知。401/403 通常需要管理者修复账号/连接，不能靠无休止重试解决。正常恢复保留发生器注册与状态，但程序从入口重新运行；失败时检查后显式重启。已有自维护脚本仍可以使用，先核对连接，不批量覆盖私人文件。
