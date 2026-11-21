from __future__ import annotations

import argparse
import asyncio
try:
    import fcntl
except ImportError:
    from relay import _windows_locks as fcntl
import json
import os
import re
import signal
from pathlib import Path

from aiohttp import web
import uvicorn

from .mcp import build
from .store import Problem, Store


def secret_file(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8" if os.name == "nt" else None) as stream:
        stream.write(value)
    path.chmod(0o600)
    if os.name == "nt":
        from relay._windows import private_file
        private_file(path)


def local_agent_url(host, port):
    host = {"0.0.0.0": "127.0.0.1", "localhost": "127.0.0.1", "::": "::1"}.get(host, host)
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


class Service:
    def __init__(self, store: Store, public_url=""):
        self.public_url = public_url
        self.mcp_url = None
        self.store = store
        self.changed = asyncio.Event()

    async def wait(self, actor: str, after: int, timeout: float):
        deadline = asyncio.get_running_loop().time() + max(0, min(timeout, 30))
        while True:
            # Clear before reading: a send between this read and wait sets this event.
            event = self.changed
            latest = self.store.latest(actor)
            remaining = deadline - asyncio.get_running_loop().time()
            if latest > after or remaining <= 0:
                return {"latest": latest, "pending": len(self.store.inbox(actor, 200))}
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except asyncio.TimeoutError:
                pass
            if self.changed is event and event.is_set():
                self.changed = asyncio.Event()


@web.middleware
async def cors(request, handler):
    origin = request.headers.get("Origin", "")
    allowed = bool(re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?", origin))
    if request.method == "OPTIONS":
        response = web.Response(status=204 if allowed else 403)
    else:
        response = await handler(request)
    if allowed:
        response.headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Vary": "Origin"})
    return response


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except Problem as error:
        response = web.json_response({"error": str(error)}, status=error.status)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        response = web.json_response({"error": "请求参数无效"}, status=400)
    if "/api/" in request.path:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def make_app(service: Service):
    store = service.store
    app = web.Application(middlewares=[cors, errors], client_max_size=128 * 1024)

    from .browser_auth import BrowserAuth
    direct_auth = {}
    @web.middleware
    async def browser_origin(request, handler):
        # Standalone uses its direct address unless explicitly configured behind a proxy.
        if service.public_url:
            request['browser_auth'] = browser_auth
        else:
            if request.host.split(':')[0] not in ('localhost', '127.0.0.1', '[::1]'):
                raise Problem('请配置 Relay public URL', 400)
            origin = request.scheme+'://'+request.host
            if origin not in direct_auth:
                if len(direct_auth)>=2:
                    raise Problem('请配置固定 Relay public URL',400)
                direct_auth[origin] = BrowserAuth(store,origin)
            request['browser_auth'] = direct_auth[origin]
        return await handler(request)
    browser_auth = BrowserAuth(store, service.public_url) if service.public_url else None
    app.middlewares.append(browser_origin)

    def me(request):
        auth = request['browser_auth']
        account = auth.actor(request)
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not auth.bearer(request):
            auth.origin_check(request)
        return account

    from .files import install as install_files
    install_files(app, service, me)

    async def browser_login(request):
        return await request['browser_auth'].handler(request)

    async def index(request):
        return web.FileResponse(Path(__file__).parent / "web/index.html")

    async def asset(request):
        return web.FileResponse(Path(__file__).parent / "web" / request.match_info["name"])

    async def state(request):
        actor = me(request)
        if not getattr(service, "ready", True):
            raise Problem("Relay 正在启动", 503)
        unread = store.rows("SELECT sender,COUNT(*) AS count FROM messages WHERE recipient=? AND group_id IS NULL AND read=0 GROUP BY sender", (actor,))
        return web.json_response({"domain": {**store.domain(), **({"mcp_url": service.mcp_url} if service.mcp_url else {})}, "me": store.account(actor), "accounts": store.accounts(actor), "groups": store.groups(actor), "unread": {r["sender"]: r["count"] for r in unread}})

    async def groups(request):
        actor = me(request)
        ident = int(request.match_info["id"]) if "id" in request.match_info else None
        if request.method == "GET":
            return web.json_response(store.group(actor, ident) if ident else {"groups": store.groups(actor)})
        body = await request.json()
        result = (store.create_group(actor, body.get("name"), body.get("members", [])) if ident is None else
                  store.manage_group(actor, ident, body.get("action"), members=body.get("members"), member=body.get("member"), name=body.get("name")))
        service.changed.set()
        return web.json_response(result)

    async def group_messages(request):
        actor, ident = me(request), int(request.match_info["id"])
        if request.method == "GET":
            return web.json_response({"messages": store.group_history(actor, ident, int(request.query.get("before", 0)), int(request.query.get("limit", 50)))})
        body = await request.json()
        result = store.send_group(actor, ident, body.get("text"), body.get("client_id", ""))
        service.changed.set()
        return web.json_response(result)

    async def accounts(request):
        actor = me(request)
        if request.method == "GET":
            return web.json_response({"accounts": store.accounts(actor)})
        body = await request.json()
        return web.json_response(store.create(actor, body.get("username", body.get("handle")), body.get("name", ""), body.get("can_create", True)))

    async def manage(request):
        actor = me(request)
        body = await request.json()
        return web.json_response(store.manage(actor, request.match_info["id"], body["action"]))

    async def messages(request):
        actor = me(request)
        if request.method == "GET":
            return web.json_response({"messages": store.history(actor, request.query["peer"], int(request.query.get("before", 0)), int(request.query.get("limit", 50)))})
        body = await request.json()
        result = store.send(actor, body["to"], body["text"], body.get("client_id", ""))
        service.changed.set()
        return web.json_response(result)

    async def receive(request):
        return web.json_response({"messages": store.inbox(me(request), int(request.query.get("limit", 50)))})

    async def ack(request):
        actor = me(request)
        body = await request.json()
        return web.json_response(store.acknowledge(actor, body["ids"], body.get("read", False)))

    async def wait(request):
        actor = me(request)
        result = await service.wait(actor, int(request.query.get("after", 0)), float(request.query.get("timeout", 25)))
        # Revalidate after a long wait so revoked credentials do not receive data.
        me(request)
        return web.json_response(result)

    async def domain(request):
        actor = me(request)
        if store.account(actor)["manager"] is not None:
            raise Problem("只有域所有者可以修改域名称", 403)
        body = await request.json()
        name = body.get("name", "").strip()
        if not name or len(name) > 60:
            raise Problem("名称须为 1–60 字")
        with store.db:
            store.db.execute("UPDATE domain SET name=?", (name,))
            store.audit(actor, "rename_domain", store.domain()["id"])
        return web.json_response(store.domain())

    async def preflight(request):
        return web.Response(status=204)

    app.router.add_get("/", index)
    app.router.add_get("/{name:auth\\.js|app\\.js|style\\.css|app-shell\\.js|manifest\\.webmanifest|icon-(?:192|512)\\.png|sw\\.js}", asset)
    app.router.add_get("/api/auth/{action:status}", browser_login)
    app.router.add_post("/api/auth/{action:.*}", browser_login)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/accounts", accounts)
    app.router.add_post("/api/accounts", accounts)
    app.router.add_post("/api/accounts/{id}", manage)
    app.router.add_get("/api/messages", messages)
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/api/groups", groups)
    app.router.add_post("/api/groups", groups)
    app.router.add_get("/api/groups/{id:[0-9]+}", groups)
    app.router.add_post("/api/groups/{id:[0-9]+}", groups)
    app.router.add_get("/api/groups/{id:[0-9]+}/messages", group_messages)
    app.router.add_post("/api/groups/{id:[0-9]+}/messages", group_messages)
    app.router.add_get("/api/receive", receive)
    app.router.add_post("/api/ack", ack)
    app.router.add_get("/api/wait", wait)
    app.router.add_post("/api/domain", domain)
    app.router.add_route("OPTIONS", "/api/{tail:.*}", preflight)
    return app


async def serve(args):
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / "instance.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("该 Relay 域已经在运行") from None
    store = Store(root / "relay.db", args.name)
    if not store.db.execute("SELECT 1 FROM accounts").fetchone():
        created = store.create(None, "user", "用户")
        secret_file(root / "owner.token", created["token"])
    service = Service(store, getattr(args, "public_url", ""))
    service.ready = False
    runner = web.AppRunner(make_app(service))
    await runner.setup()
    mcp = uvicorn.Server(uvicorn.Config(build(service), host=args.host, port=args.mcp_port, log_level="warning"))
    mcp_task = asyncio.create_task(mcp.serve())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name == 'nt':
        from relay._windows import install_stop_handler
        install_stop_handler(asyncio.get_running_loop(), stop.set)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    try:
        await web.TCPSite(runner, args.host, args.port).start()
        # Publish before ready, using the bound port (also supports --port 0).
        store.publish_agent_url(getattr(args, "agent_url", "") or local_agent_url(args.host, runner.addresses[0][1]))
        for _ in range(100):
            if mcp.started:
                break
            if mcp_task.done():
                await mcp_task
                raise RuntimeError("Relay MCP 启动失败")
            await asyncio.sleep(.05)
        if not mcp.started:
            raise RuntimeError("Relay MCP 启动超时")
        mcp_port = mcp.servers[0].sockets[0].getsockname()[1]
        service.mcp_url = getattr(args, "agent_mcp_url", "") or local_agent_url(args.host, mcp_port) + "/mcp"
        service.ready = True
        if os.name == 'nt':
            # Uvicorn installs its handlers while starting; the host owns final
            # shutdown so Ctrl+C must also release HTTP, SQLite and the lock.
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        print(f"Relay: http://{args.host}:{args.port}  MCP: http://{args.host}:{args.mcp_port}/mcp", flush=True)
        print(f"网页登录凭据文件: {root / 'owner.token'}", flush=True)
        await stop.wait()
    finally:
        await runner.cleanup()
        mcp.should_exit = True
        await mcp_task
        store.db.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Relay 独立域聊天")
    parser.add_argument("--root", default=os.environ.get("RELAY_HOME", "~/.relay"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19920)
    parser.add_argument("--mcp-port", type=int, default=19921)
    parser.add_argument("--name", default="Relay")
    parser.add_argument("--public-url", default="", help="External Relay URL including any reverse-proxy path")
    parser.add_argument("--agent-mcp-url", default="", help="MCP endpoint advertised to agents (defaults to the local listener)")
    parser.add_argument("--agent-url", default="", help="Override the host-published agent endpoint; default: same-host listener")
    asyncio.run(serve(parser.parse_args()))
