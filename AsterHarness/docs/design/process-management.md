# Session 进程管理

每个 live Session 有独立的 UnifiedExecProcessManager。`exec_command` 创建后台命令后返回的 `session_id` 是管理器句柄，不是 Linux PID。宿主通过该管理器启动的服务同样可查询，既不要求模型持有初始调用回执，也不依赖在另一个命令沙箱中执行 ps/kill。

## 工具

- `list_processes()`：只列当前 Session 登记、尚未收取最终输出的命令。字段为 session_id、command、workdir、tty、status、exit_code、output_pending；不消费输出，不扫描整机进程。已退出但尚未收取输出的命令仍可显示。
- `write_stdin(session_id, chars="")`：读取后续输出；普通输入需要原命令 tty=true。单独的 U+0003（Ctrl+C）对 TTY 与非 TTY 命令都可用，由宿主向登记的进程组发送 SIGINT。中断不保证程序退出，程序可以捕获或忽略信号。
- `stop_process(session_id, max_output_tokens=10000)`：对登记的进程组发送 SIGTERM，等待后必要时升级 SIGKILL，收取剩余输出并移除句柄。返回 stopped/already_exited、stopped_session_id、exit_code 和 output。termination_pending 表示尚未确认退出，不是成功。输出沿用统一工具预算。

这些工具使用普通工具回执进入事件队列，在后续 Step 中可见。已注册的命令完成发生器仍报告实际退出，不会因 stop 自动取消。程序事件发生器单独由 cancel_event_generator / restart_program_generator 管理，不属于命令句柄列表。

停止不等待另一个长轮询超时：先对同一进程对象完成清理，再在交互锁下收取输出。轮询已经收取的内容不会重复返回。并发停止、轮询、Session 关闭共用一次幂等清理，避免重复关闭 PTY。

## 权限与生命周期

启用 Linux/WSL 沙箱时，每个命令位于自己的 PID namespace。文件路径 `/` 可写不改变 PID 隔离。网络未隔离时，另一个命令可以访问服务端口，但不能据此操作服务所属的宿主 PID。

宿主工具只从当前管理器查找句柄；不能通过传入另一 Session 的句柄或宿主 PID，接管那里的进程。不会因为新增工具而赋予 root 权限。若关闭沙箱，普通 shell 命令能操作什么仍取决于宿主 Linux 用户权限；工具句柄的归属限制本身不等于整机隔离。

Session 关闭会停止登记的命令。句柄不持久化，resume 不恢复旧进程；当前 Session 的 list 不会重新发现任意遗留服务。没有自动收养 PID 的能力。长期业务服务可以通过程序发生器或明确的宿主启动约定重新启动。

进程组停止不是对所有自行脱离进程组的后代的通用管理接口。沙箱命名空间的退出另有清理边界。这里不提供任意整机进程管理、跨 Agent 控制或自动服务重启策略。

## 验证

真实 Linux 测试覆盖：非 TTY SIGINT 处理、句柄元信息与输出保留、跨 Session 拒绝、长轮询期间停止、忽略 TERM 后升级 KILL、完成事件、宿主代启动命令的模型工具查询，以及两个独立沙箱 HTTP 服务互不干扰地停止并释放端口。
