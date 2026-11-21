import contextvars
import json

from mcp.server.fastmcp import FastMCP

from .store import Problem

CALLER = contextvars.ContextVar("relay_caller", default=None)


class Auth:
    def __init__(self, app, resolve):
        self.app, self.resolve = app, resolve

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"authorization", b"").decode()
        try:
            actor = self.resolve(raw[7:] if raw.startswith("Bearer ") else "")
        except Problem as error:
            body = json.dumps({"error": str(error)}).encode()
            await send({"type": "http.response.start", "status": error.status, "headers": [(b"content-type", b"application/json")]})
            return await send({"type": "http.response.body", "body": body})
        current = CALLER.set(actor)
        try:
            await self.app(scope, receive, send)
        finally:
            CALLER.reset(current)


def build(service):
    store = service.store
    mcp = FastMCP("relay", stateless_http=True, json_response=True, log_level="WARNING",
                  instructions="Relay 是独立域聊天软件。发送者由凭据确定。读取消息不删除；接收后显式确认。详见 relay skill。")

    def me():
        return CALLER.get()["id"]

    @mcp.tool()
    async def relay_me() -> dict:
        """查询这次连接已经登录的域和用户名。必须先配置服务地址和账号凭据；本工具不创建域、账号，也不执行登录。"""
        return {"domain": {"name": store.domain()["name"]}, "account": store.public_account(store.account(me()))}

    @mcp.tool()
    async def relay_accounts() -> dict:
        """列出已登录域中的账号：唯一 username、显示名称、活跃状态和是否可管理。显示名称可以重复，username 不可重复。"""
        return {"accounts": [store.public_account(a) for a in store.accounts(me())]}

    @mcp.tool()
    async def relay_send(to: str, text: str, client_id: str = "") -> dict:
        """发送私聊。to 使用域内唯一用户名，如 lin 或 @lin，不需要内部 ID。client_id 用于幂等重试。"""
        result = store.send(me(), to, text, client_id)
        service.changed.set()
        return store.public_message(result)

    @mcp.tool()
    async def relay_receive(limit: int = 50) -> dict:
        """读取自己的私聊和群聊待接收消息，不自动确认。sender/recipient 是可信用户名，id 用于 ack。kind=group 时按 group_id 用 relay_send_group 在原群回复，不要误回私聊；群消息不要求人人回复。"""
        return {"messages": [store.public_message(m) for m in store.inbox(me(), limit)]}

    @mcp.tool()
    async def relay_ack(ids: list[int], read: bool = False) -> dict:
        """确认自己收到的消息；read=true 同时标记聊天已读。确认不代表任务完成。"""
        return store.acknowledge(me(), ids, read)

    @mcp.tool()
    async def relay_history(peer: str, before: int = 0, limit: int = 50) -> dict:
        """peer 使用域内用户名，如 @lin。读取自己参与的私聊，before 消息编号用于向前分页。"""
        return {"messages": [store.public_message(m) for m in store.history(me(), peer, before, limit)]}

    def public_group(group):
        result = {key: group[key] for key in ("id", "name", "can_manage", "can_send", "muted", "member_muted", "member_count")}
        result["owner"] = group["owner_username"]
        if "members" in group:
            result["members"] = [{key: member[key] for key in ("username", "name", "muted", "active")} for member in group["members"]]
        if "unread" in group:
            result["unread"] = group["unread"]
        return result

    @mcp.tool()
    async def relay_groups() -> dict:
        """列出自己参与的群聊和群编号、禁言及未读状态。群成员关系独立于账号管理关系。"""
        return {"groups": [public_group(group) for group in store.groups(me())]}

    @mcp.tool()
    async def relay_create_group(name: str, members: list[str] | None = None) -> dict:
        """创建群聊并邀请同域用户名，自己成为群主。每群最多100人。新成员仅能查看入群之后的消息。"""
        result = store.create_group(me(), name, members)
        service.changed.set()
        return public_group(result)

    @mcp.tool()
    async def relay_group(group: int) -> dict:
        """查看自己所在群的名称、成员、群主和发言权限。group 为 relay_groups 返回的群编号。"""
        return public_group(store.group(me(), group))

    @mcp.tool()
    async def relay_manage_group(group: int, action: str, members: list[str] | None = None, member: str | None = None, name: str | None = None) -> dict:
        """群主操作：invite(members用户名列表)、remove/mute/unmute(member用户名)、rename(name)、mute_all/unmute_all、dissolve。普通成员可 leave；群主离开需解散。"""
        result = store.manage_group(me(), group, action, members=members, member=member, name=name)
        service.changed.set()
        return public_group(result) if "members" in result else result

    @mcp.tool()
    async def relay_send_group(group: int, text: str, client_id: str = "") -> dict:
        """发群消息，向除自己外的所有当前成员投递。禁言后不可发送，不要转私聊绕过。重试使用相同 client_id。"""
        result = store.send_group(me(), group, text, client_id)
        service.changed.set()
        return store.public_group_message(result)

    @mcp.tool()
    async def relay_group_history(group: int, before: int = 0, limit: int = 50) -> dict:
        """读取自己入群后的群历史；before 是群消息编号。历史不自动确认待收队列；请用 relay_receive 的消息 id 执行 ack。"""
        return {"messages": [store.public_group_message(m) for m in store.group_history(me(), group, before, limit)]}

    @mcp.tool()
    async def relay_create_account(username: str, name: str = "", can_create: bool = True) -> dict:
        """在已登录的域创建账号。username 域内唯一、不区分大小写、创建后不可更改；name 是可重复显示名称。返回当次明文凭据。"""
        result = store.create(me(), username, name, can_create)
        return {"account": store.public_account(result["account"]), "token": result["token"]}

    @mcp.tool()
    async def relay_manage_account(account: str, action: str) -> dict:
        """account 使用域内用户名，如 @lin。只管理自己创建的账号及其下属账号。action: disable/enable/reset_token。"""
        return store.manage(me(), account, action)

    return Auth(mcp.streamable_http_app(), store.authenticate)
