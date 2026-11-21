from mcp.server.fastmcp import FastMCP

from relay.mcp import Auth, CALLER


def build(service):
    mcp = FastMCP("squad", stateless_http=True, json_response=True, log_level="WARNING",
                  instructions="Squad 提供 agent 管理，日常聊天使用独立 Relay。身份只认连接凭据，使用详情见 squad skill。")

    def me():
        return CALLER.get()["id"]

    @mcp.tool()
    async def squad_host_systemd(program: str, arguments: list[str], timeout: int = 30) -> dict:
        """获授权限后，直接管理宿主当前用户的全部 systemd 服务。program 为 systemctl/journalctl/systemd-run，arguments 为参数数组，自动添加 --user，不经 shell。可 link 项目内 unit、daemon-reload、enable/start/restart、读取日志或启动常驻任务。此能力可在宿主用户身份下执行代码，超出路径沙箱；不含 root/Windows 权限。调用前读 squad skill。"""
        return await service.host_systemd(me(), program, arguments, timeout)

    @mcp.tool()
    async def squad_self() -> dict:
        """查询自己的有效配置、稳定 ID、workspace、parent、Relay 账号和宿主允许的模型。"""
        return {"agent": service.view(service.store.agent(me())), "models": service.models, "mode": service.mode}

    @mcp.tool()
    async def squad_list() -> dict:
        """Squad 名册：稳定 ID、名称、parent、运行状态和 Relay 账号。不公开他人 instruction 或凭据。"""
        return {"agents": [{k: a[k] for k in ("id", "handle", "parent", "status", "relay_username")} for a in map(service.view, service.store.agents())]}

    @mcp.tool()
    async def squad_create(handle: str, instruction: str, model: str = "", grants: list[dict] | None = None, allow_connect: bool = False, gpu_access: bool = False, local_ipc: bool = False, host_systemd: bool = False, sandbox_enabled: bool = True, mcp_services: list[str] | None = None, auto_start: bool = False) -> dict:
        """创建自己的直接 child，自动开户、挂载 Squad/Relay MCP 和 skills、启动。提供身份职责；路径、GPU、本机进程通信、systemd、关闭沙箱和额外 MCP 只能在自己的授权内转授。root 不可转授。auto_start 默认关闭，开启后仅在空间重启时恢复仍请求运行且无错误的成员。"""
        return await service.create(me(), handle, instruction, model, grants, allow_connect=allow_connect, gpu_access=gpu_access, local_ipc=local_ipc, host_systemd=host_systemd, sandbox_enabled=sandbox_enabled, mcp_services_ids=mcp_services, auto_start=auto_start)

    @mcp.tool()
    async def squad_configure(agent: str, instruction: str | None = None, model: str | None = None, grants: list[dict] | None = None, allow_connect: bool | None = None, gpu_access: bool | None = None, local_ipc: bool | None = None, host_systemd: bool | None = None, sandbox_enabled: bool | None = None, mcp_services: list[str] | None = None, auto_start: bool | None = None) -> dict:
        """配置自己的直接 child。grants 提交完整列表，只调整自己权限范围内的部分，保留范围外的已有授权；grants=[] 清除全部额外授权也须有权。仅改路径时在线应用，pending 不代表生效，查 permission_update；修改模型或 instruction 时正常暂停并恢复原 Thread。GPU、本机进程通信、systemd、关闭沙箱和 MCP 也只允许调整自己拥有的部分；mcp_services 为完整 ID 列表。收窄前先收回下级对应授权；root 不可转授。auto_start 控制重启恢复，修改该开关不重启成员；主动暂停后不会自动拉起，失败不自动重试。省略字段保留。"""
        return await service.configure(me(), agent, instruction=instruction, model=model, grants=grants, allow_connect=allow_connect, gpu_access=gpu_access, local_ipc=local_ipc, host_systemd=host_systemd, sandbox_enabled=sandbox_enabled, mcp_services_ids=mcp_services, auto_start=auto_start)

    @mcp.tool()
    async def squad_start(agent: str) -> dict:
        """启动自己的直接 child，恢复原 Thread；失败明确报告，不自动换 Thread。"""
        return await service.start(me(), agent)

    @mcp.tool()
    async def squad_stop(agent: str) -> dict:
        """正常暂停自己的直接 child，保留 Thread、记忆与 workspace。"""
        return await service.stop(me(), agent)

    @mcp.tool()
    async def squad_reconnect_relay(agent: str) -> dict:
        """修复直接 child 的聊天连接：重置并重新注入其账号凭据，运行中则暂停后恢复同一 Thread。不返回凭据。"""
        return await service.reconnect_relay(me(), agent)

    @mcp.tool()
    async def squad_send(to: str, text: str) -> dict:
        """给 parent 或直接 child 发管理消息，落库并尝试唤醒，离线时补投；不等于完成任务。"""
        return await service.send(me(), to, text)

    @mcp.tool()
    async def squad_inbox(limit: int = 50) -> dict:
        """自己最近收发的管理消息；accepted 只表示 worker 接收入队，不代表模型已读。"""
        from relay.store import Store
        return {"messages": service.store.rows("SELECT * FROM messages WHERE (sender=? OR recipient=?) AND kind='management' ORDER BY id DESC LIMIT ?", (me(), me(), Store.limit(limit)))}

    return Auth(mcp.streamable_http_app(), service.store.authenticate)
