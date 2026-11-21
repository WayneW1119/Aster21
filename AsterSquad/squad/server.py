from __future__ import annotations
from .paths import default_root

import argparse
import asyncio
import json
import os
if os.name == "nt":
    from squad._windows import spawn as windows_spawn
try:
    import fcntl
except ImportError:
    from squad import _windows_locks as fcntl
import secrets
import signal
import sys
from pathlib import Path

import aiohttp
from aiohttp import web
import uvicorn

from relay.server import errors, secret_file
from relay.store import Problem
from .mcpserver import build
from .service import Service
from .models import load_catalog
from .mcp_services import load_catalog as load_mcp_catalog


def make_app(service, owner_token):
    @web.middleware
    async def auth(request, handler):
        if request.path.startswith("/api/"):
            raw = request.headers.get("Authorization", "")
            token = raw[7:] if raw.startswith("Bearer ") else ""
            if not secrets.compare_digest(token, owner_token):
                raise Problem("需要 Squad 用户凭据", 401)
        return await handler(request)

    app = web.Application(middlewares=[errors, auth], client_max_size=128 * 1024)

    async def index(request):
        return web.FileResponse(Path(__file__).parent / "web/index.html")

    async def state(request):
        if not getattr(service, "ready", True):
            raise Problem("Squad 正在启动", 503)
        return web.json_response(service.snapshot())

    async def create(request):
        body = await request.json()
        result = await service.create("owner", body["handle"], body["instruction"], body.get("model", ""), body.get("grants", []), parent=body.get("parent"), sandbox_enabled=body.get("sandbox_enabled", True), mcp_services_ids=body.get("mcp_services"), run_as_root=body.get('run_as_root', False), allow_connect=body.get('allow_connect', False), gpu_access=body.get('gpu_access', False), local_ipc=body.get('local_ipc', False), host_systemd=body.get('host_systemd', False), windows_admin=body.get('windows_admin'), auto_start=body.get('auto_start', False))
        return web.json_response(result)

    async def action(request):
        ident, action = request.match_info["id"], request.match_info["action"]
        body = await request.json()
        if action == "configure":
            result = await service.configure("owner", ident, instruction=body.get("instruction"), model=body.get("model"), grants=body.get("grants"), host_systemd=body.get("host_systemd"), sandbox_enabled=body.get("sandbox_enabled"), mcp_services_ids=body.get("mcp_services"), run_as_root=body.get('run_as_root'), allow_connect=body.get('allow_connect'), gpu_access=body.get('gpu_access'), local_ipc=body.get('local_ipc'), windows_admin=body.get('windows_admin'), auto_start=body.get('auto_start'))
        elif action in {"start", "stop"}:
            result = await getattr(service, action)("owner", ident)
        elif action == "reconnect":
            result = await service.reconnect_relay("owner", ident)
        elif action in {"archive", "restore"}:
            result = await service.archive("owner", ident, restore=action == "restore")
        elif action == "terminal":
            result = await service.send("owner", ident, body["text"], terminal=True)
        else:
            raise Problem("未知操作", 404)
        return web.json_response(result)

    async def terminal(request):
        ident = request.match_info["id"]
        service.store.agent(ident)
        return web.json_response({"messages": service.store.rows("SELECT * FROM terminal WHERE agent=? ORDER BY id DESC LIMIT 100", (ident,))[::-1]})

    async def relay_login(request):
        # Only the authenticated owner can obtain their own Relay login link.
        return web.json_response({"url": service.relay_url + "/#token=" + service.owner_relay_token})

    async def bulk(request):
        body = await request.json()
        return web.json_response(await service.bulk(body["action"]))

    async def settings(request):
        body = await request.json()
        name = body.get("name", getattr(service, 'name', 'Squad'))
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise Problem("名称须为 1–60 字")
        if 'restore_members' in body and type(body['restore_members']) is not bool:
            raise Problem('restore_members 须为布尔值')
        if 'name' in body:
            await service.relay(service.owner_relay_token, "POST", "/api/domain", {"name": name.strip()})
            service.name = name.strip()
        if 'restore_members' in body:
            service.store.set_restore_members(body['restore_members'])
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/agents", create)
    app.router.add_post("/api/agents/{id}/{action}", action)
    app.router.add_get("/api/agents/{id}/terminal", terminal)
    app.router.add_get("/api/relay-login", relay_login)
    app.router.add_post("/api/bulk", bulk)
    app.router.add_post("/api/settings", settings)
    return app


async def serve(args):
    root = Path(args.root).expanduser().resolve()
    private = root / "control"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (private / "instance.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("该 Squad 数据目录已经在运行") from None
    token_file = private / "owner.token"
    if not token_file.exists():
        secret_file(token_file, secrets.token_urlsafe(32))
    owner_token = token_file.read_text(encoding="utf-8").strip()
    relay_url = f"http://{args.host}:{args.relay_port}"
    relay_mcp = f"http://{args.host}:{args.relay_mcp_port}/mcp"
    relay_root = private / "relay"
    relay_log = (private / "relay.log").open("ab")
    relay = await (windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(sys.executable, "-P", "-m", "relay", "--root", str(relay_root),
        "--host", args.host, "--port", str(args.relay_port), "--mcp-port", str(args.relay_mcp_port), "--name", args.name,
        "--public-url", getattr(args, "relay_public_url", ""),
        stdout=relay_log, stderr=relay_log)
    relay_log.close()
    service = runner = mcp = mcp_task = restore_task = None
    stop = asyncio.Event()
    if os.name == 'nt':
        from squad._windows import install_stop_handler
        install_stop_handler(asyncio.get_running_loop(), stop.set)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                if relay.returncode is not None:
                    raise RuntimeError("Relay 启动失败，请查看 control/relay.log")
                try:
                    relay_token = (relay_root / "owner.token").read_text(encoding="utf-8").strip()
                    async with session.get(relay_url + "/api/state", headers={"Authorization": "Bearer " + relay_token}) as response:
                        if response.status == 200:
                            state = await response.json()
                            break
                except (OSError, aiohttp.ClientError):
                    pass
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("Relay 启动超时")
        catalog = load_catalog(args.model_catalog)
        models = ["fake-echo"] if args.mode == "fake" else [s.strip() for s in args.models.split(",") if s.strip()]
        if args.mode == "real" and catalog:
            models = list(catalog)
        if not models:
            raise RuntimeError("真实模式需要 --models 或 SQUAD_MODEL")
        service = Service(root, mode=args.mode, relay_url=relay_url, relay_mcp=relay_mcp,
            squad_mcp=f"http://{args.host}:{args.mcp_port}/mcp", owner_relay_token=relay_token,
            domain=state["domain"]["id"], user_account=state["me"]["id"], models=models,
            model_url=args.responses_url, allow_http=args.allow_http, user_username=state["me"]["username"], private_roots=tuple(Path(p) for p in args.private_root), model_catalog=catalog, shared_skill_root=Path(args.shared_skill_root) if args.shared_skill_root else root,
            mcp_catalog=load_mcp_catalog(getattr(args, "mcp_catalog", "")))
        service.mcp_catalog_path = getattr(args, "mcp_catalog", "")
        service.domain_name = state["domain"]["name"]
        service.name = args.name
        service.ready = False
        mcp = uvicorn.Server(uvicorn.Config(build(service), host=args.host, port=args.mcp_port, log_level="warning"))
        mcp_task = asyncio.create_task(mcp.serve())
        runner = web.AppRunner(make_app(service, owner_token))
        await runner.setup()
        await web.TCPSite(runner, args.host, args.port).start()
        for _ in range(100):
            if mcp.started:
                break
            if mcp_task.done():
                await mcp_task
                raise RuntimeError("Squad MCP 启动失败")
            await asyncio.sleep(.05)
        if not mcp.started:
            raise RuntimeError("Squad MCP 启动超时")
        service.ready = True
        restore_task = asyncio.create_task(service.restore_on_startup())
        print(f"Squad: http://{args.host}:{args.port}  Relay: {relay_url}", flush=True)
        print(f"Squad 用户凭据文件: {token_file}", flush=True)
        stop_task = asyncio.create_task(stop.wait())
        relay_wait = asyncio.create_task(relay.wait())
        done, pending = await asyncio.wait({stop_task, relay_wait, mcp_task}, return_when=asyncio.FIRST_COMPLETED)
        if relay_wait in done and not stop.is_set():
            raise RuntimeError("Relay 意外退出；正在正常暂停所有 agent")
        for task in pending:
            if task is not mcp_task:
                task.cancel()
        await asyncio.gather(*(t for t in pending if t is not mcp_task), return_exceptions=True)
    finally:
        # Keep MCP and Relay alive until all agents have checkpointed.
        if runner:
            await runner.cleanup()
        if service:
            await service.close()  # Marks closing before draining starts already in flight.
        if restore_task:
            await asyncio.gather(restore_task, return_exceptions=True)
        if mcp:
            mcp.should_exit = True
        if mcp_task:
            await mcp_task
        if relay.returncode is None:
            __import__('squad._windows',fromlist=['request_stop']).request_stop(relay) if os.name == 'nt' else relay.terminate()
            try:
                await asyncio.wait_for(relay.wait(), 10)
            except asyncio.TimeoutError:
                relay.kill()
                await relay.wait()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Aster Squad · resident agent domain")
    parser.add_argument("--root", default=default_root())
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=19910)
    parser.add_argument("--mcp-port", type=int, default=19911)
    parser.add_argument("--relay-port", type=int, default=19920)
    parser.add_argument("--relay-mcp-port", type=int, default=19921)
    parser.add_argument("--relay-public-url", default="")
    parser.add_argument("--name", default="Aster Studio")
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    parser.add_argument("--models", default=os.environ.get("SQUAD_MODEL", ""))
    parser.add_argument("--responses-url", default=os.environ.get("SQUAD_RESPONSES_URL", "https://api.openai.com/v1/responses"))
    parser.add_argument("--model-catalog", default=os.environ.get("SQUAD_MODEL_CATALOG", ""))
    parser.add_argument("--mcp-catalog", default=os.environ.get("SQUAD_MCP_CATALOG", ""))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--shared-skill-root", default="", help="共享 skills 所属的 Squad 数据根目录")
    parser.add_argument("--private-root", action="append", default=[], help="宿主额外强制隐藏的目录")
    args = parser.parse_args()
    ports = [args.port, args.mcp_port, args.relay_port, args.relay_mcp_port]
    if len(set(ports)) != 4:
        parser.error("四个服务端口必须不同")
    asyncio.run(serve(args))
