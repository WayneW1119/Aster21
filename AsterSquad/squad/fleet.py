"""Local console supervising independent Squads and standalone Relay domains."""
from __future__ import annotations
from .paths import default_root

import argparse
import asyncio
import os
if os.name == "nt":
    from squad._windows import spawn as windows_spawn
try:
    import fcntl
except ImportError:
    from squad import _windows_locks as fcntl
import json
import os
import secrets
import signal
import socket
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
import html
import re

import aiohttp
from aiohttp import web
from relay.server import errors, secret_file
from relay.store import Problem
from .models import load_catalog
from .mcp_services import load_catalog as load_mcp_catalog


def available_ports(count, reserved=()):
    sockets = []
    try:
        while len(sockets) < count:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            if sock.getsockname()[1] in reserved:
                sock.close()
                continue
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Fleet:
    def __init__(self, root, args):
        self.root, self.args = Path(root).resolve(), args
        from .startup import Startup
        self.startup = Startup(unit=getattr(args, 'startup_unit', ''), desktop_root=getattr(args, 'desktop_root', ''))
        from .skills import install_builtins
        install_builtins(self.root)
        self.model_catalog = load_catalog(getattr(args, "model_catalog", ""))
        load_mcp_catalog(getattr(args, "mcp_catalog", ""))
        self.private = self.root / "control/fleet"
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.private / "console.db")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS resources(
          id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
          kind TEXT NOT NULL, root TEXT NOT NULL UNIQUE, ports TEXT NOT NULL,
          mode TEXT NOT NULL, description TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.processes, self.locks, self.cache = {}, {}, {}
        self.ready = set()
        self.stopping = set()
        self.creation_lock = asyncio.Lock()
        for folder in [self.root / "workspaces", self.root / "runtime", self.root / "connections", self.root / "instances"]:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.db.execute("SELECT 1 FROM resources").fetchone() and not self.db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
            self.insert("studio", args.name, "squad", self.root,
                        [args.primary_port, args.mcp_port, args.relay_port, args.relay_mcp_port], args.mode, "默认工作室")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('initialized','1')")

    def insert(self, ident, name, kind, root, ports, mode, description):
        try:
            with self.db:
                self.db.execute("INSERT INTO resources(id,name,kind,root,ports,mode,description,created) VALUES(?,?,?,?,?,?,?,?)",
                    (ident, name, kind, str(root), json.dumps(ports), mode, description, time.time()))
        except sqlite3.IntegrityError:
            raise Problem("该名称已存在", 409) from None

    def records(self):
        return [self.record(r[0]) for r in self.db.execute("SELECT id FROM resources ORDER BY created")]

    def record(self, ident):
        row = self.db.execute("SELECT * FROM resources WHERE id=?", (ident,)).fetchone()
        if not row:
            raise Problem("Squad 或域不存在", 404)
        result = dict(row)
        result["ports"] = json.loads(result["ports"])
        return result

    def live(self, ident):
        return ident in self.ready and self.running(ident)

    def running(self, ident):
        proc = self.processes.get(ident)
        return proc is not None and proc.returncode is None

    @staticmethod
    def token_file(record, relay=False):
        root = Path(record["root"])
        if record["kind"] == "domain":
            return root / "owner.token"
        return root / ("control/relay/owner.token" if relay else "control/owner.token")

    @staticmethod
    def relay_url(record):
        return f"http://127.0.0.1:{record['ports'][2 if record['kind'] == 'squad' else 0]}"

    def web_origin(self):
        return getattr(self.args, "public_url", "").rstrip("/") or f"http://127.0.0.1:{self.args.port}"

    def relay_web_url(self, ident):
        return self.web_origin() + "/relay/domains/" + quote(ident, safe="")

    def browser_data(self, data):
        """Public pages use stable same-origin addresses, never internal ports."""
        if isinstance(data, list):
            return [self.browser_data(item) for item in data]
        if not isinstance(data, dict):
            return data
        result = {k:self.browser_data(v) for k,v in data.items()}
        if "relay_url" in result:
            value = next((r for r in self.records() if self.relay_url(r) == result["relay_url"]), None)
            if value:
                result["relay_url"] = self.relay_web_url(value["id"])
        return result

    async def call(self, record, path="state", method="GET", body=None):
        if not self.running(record["id"]):
            raise Problem("此服务已暂停，请先启动", 409)
        token = self.token_file(record).read_text(encoding="utf-8").strip()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            try:
                async with session.request(method, f"http://127.0.0.1:{record['ports'][0]}/api/{path}",
                    headers={"Authorization": "Bearer " + token}, json=body) as response:
                    data = await response.json()
                    if response.status >= 400:
                        raise Problem(data.get("error", "服务请求失败"), response.status)
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise Problem("暂时无法连接服务，请检查运行状态", 502) from None

    async def start(self, ident):
        async with self.locks.setdefault(ident, asyncio.Lock()):
            record = self.record(ident)
            if ident in self.stopping and self.running(ident):
                raise Problem("空间正在安全暂停，请等待完成后再启动", 409)
            if self.live(ident):
                return self.public(record)
            with self.db:
                self.db.execute("UPDATE resources SET enabled=1 WHERE id=?", (ident,))
            command = [sys.executable, "-P", "-m", "squad" if record["kind"] == "squad" else "relay", "--root", record["root"], "--name", record["name"],
                       "--port", str(record["ports"][0]), "--mcp-port", str(record["ports"][1])]
            if record["kind"] == "squad":
                command.extend(["--relay-port", str(record["ports"][2]), "--relay-mcp-port", str(record["ports"][3]), "--mode", record["mode"],
                    "--models", self.args.models, "--responses-url", self.args.responses_url,
                    "--relay-public-url", self.relay_web_url(ident)])
                command.extend(["--shared-skill-root", str(self.root)])
                if getattr(self.args, "model_catalog", ""):
                    command.extend(["--model-catalog", str(Path(self.args.model_catalog).resolve())])
                if getattr(self.args, "mcp_catalog", ""):
                    command.extend(["--mcp-catalog", str(Path(self.args.mcp_catalog).resolve())])
                if self.args.allow_http:
                    command.append("--allow-http")
                if Path(record["root"]) == self.root:
                    protected = [self.root / "instances"]
                else:
                    protected = [self.root / "control", self.root / "workspaces", self.root / "runtime", self.root / "connections"]
                for path in protected:
                    command.extend(["--private-root", str(path)])
            if record["kind"] == "domain":
                command.extend(["--public-url", self.relay_web_url(ident)])
            log_path = self.private / "logs" / f"{ident}.log"
            log_path.parent.mkdir(exist_ok=True)
            with log_path.open("ab") as log:
                proc = await (windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(*command, stdout=log, stderr=log, start_new_session=True)
            self.processes[ident] = proc
            self.stopping.discard(ident)
            self.ready.discard(ident)
            for _ in range(150):
                if proc.returncode is not None:
                    break
                try:
                    data = await self.call(record)
                    self.cache[ident] = data
                    await self.call(record, "settings" if record["kind"] == "squad" else "domain", "POST", {"name": record["name"]})
                    self.ready.add(ident)
                    return self.public(record)
                except (Problem, OSError):
                    await asyncio.sleep(.1)
            await self.stop_process(ident)
            raise Problem("服务启动失败，详情见 control/fleet/logs/" + ident + ".log", 502)

    async def stop_process(self, ident, *, timeout=55):
        proc = self.processes.get(ident)
        if proc and proc.returncode is None:
            if ident not in self.stopping:
                __import__('squad._windows',fromlist=['request_stop']).request_stop(proc) if os.name == 'nt' and isinstance(getattr(proc,'_aster_stop_event',None),str) else proc.terminate()
            self.stopping.add(ident)
            self.ready.discard(ident)
            try:
                await asyncio.wait_for(proc.wait(), timeout)
            except asyncio.TimeoutError:
                raise Problem("空间仍在等待成员安全暂停；未强制结束，请稍后重试", 409)
        self.processes.pop(ident, None)
        self.stopping.discard(ident)
        self.ready.discard(ident)

    async def stop(self, ident):
        async with self.locks.setdefault(ident, asyncio.Lock()):
            await self.detail(ident)
            with self.db:
                self.db.execute("UPDATE resources SET enabled=0 WHERE id=?", (ident,))
            await self.stop_process(ident)
        return self.public(self.record(ident))

    async def remove(self, ident):
        async with self.locks.setdefault(ident, asyncio.Lock()):
            record = self.record(ident)
            await self.detail(ident)
            with self.db:
                self.db.execute('UPDATE resources SET enabled=0 WHERE id=?', (ident,))
            # A timeout retains registration and files for an explicit retry.
            await self.stop_process(ident)
            async with self.creation_lock:
                record = self.record(ident)
                record['enabled'] = 0
                secret_file(self.private/'removed'/f'{ident}.json', json.dumps(record, ensure_ascii=False))
                with self.db:
                    self.db.execute('DELETE FROM resources WHERE id=?', (ident,))
            self.cache.pop(ident, None)
        return {'ok': True, 'data_preserved': True}

    def public(self, record):
        data = self.cache.get(record["id"], {})
        agents = [a for a in data.get("agents", []) if not a.get("archived")]
        return {k: record[k] for k in ("id", "name", "kind", "mode", "description", "created")} | {
            "status": "stopping" if record["id"] in self.stopping and self.running(record["id"]) else "running" if self.live(record["id"]) else "starting" if self.running(record["id"]) else "stopped",
            "relay_url": self.relay_url(record), "agents": len(agents),
            "online": sum(a["status"] == "online" for a in agents) if self.live(record["id"]) else 0,
            "members": len(data.get("accounts", [])),
            "restore_members": self.restore_policy(record),
        }

    def restore_policy(self, record):
        if record['kind'] != 'squad':
            return False
        path = Path(record['root']) / 'control/squad.db'
        if not path.exists():
            return False
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='settings'").fetchone():
                row = db.execute("SELECT value FROM settings WHERE key='restore_members'").fetchone()
                if row:
                    return json.loads(row[0])
            return any(json.loads(row[0]).get('auto_start') is True for row in db.execute('SELECT config FROM agents'))
        finally:
            db.close()

    async def detail(self, ident):
        record = self.record(ident)
        if self.live(ident):
            try:
                self.cache[ident] = await self.call(record)
                secret_file(self.private / "snapshots" / f"{ident}.json", json.dumps(self.cache[ident], ensure_ascii=False))
            except Problem as error:
                return {"resource": self.public(record), "error": str(error), **self.cache.get(ident, {})}
        elif ident not in self.cache:
            path = self.private / "snapshots" / f"{ident}.json"
            if path.exists():
                self.cache[ident] = json.loads(path.read_text(encoding="utf-8"))
        data = dict(self.cache.get(ident, {}))
        if record['kind'] == 'squad':
            data['restore_members'] = self.restore_policy(record)
        if not self.live(ident) and "agents" in data:
            data["agents"] = [{**a, "status": "archived" if a.get("archived") else "offline"} for a in data["agents"]]
        return {**data, "resource": self.public(record)}

    async def state(self):
        details = await asyncio.gather(*(self.detail(r["id"]) for r in self.records()))
        by_id = {d["resource"]["id"]: d for d in details}
        primary = by_id.get("studio", {})
        return {**primary, "resources": [d["resource"] for d in details], "models": list(self.model_catalog) or [m.strip() for m in self.args.models.split(",") if m.strip()],
                "manager": True}

    async def create(self, kind, name, description="", mode="fake", restore_members=False):
        if type(restore_members) is not bool or (kind != 'squad' and restore_members):
            raise Problem('restore_members 须为 Squad 的布尔值设置')
        if kind not in {"squad", "domain"} or not isinstance(name, str) or not name.strip() or len(name) > 60:
            raise Problem("请填写 1–60 字的名称")
        if not isinstance(description, str) or len(description) > 1000 or mode not in {"fake", "real"}:
            raise Problem("配置无效")
        if kind == "squad" and mode == "real" and not (self.model_catalog or self.args.models):
            raise Problem("请先在宿主配置可用的真实模型", 409)
        async with self.creation_lock:
            if self.db.execute('SELECT 1 FROM resources WHERE name=?', (name.strip(),)).fetchone():
                raise Problem('该名称已存在', 409)
            ident = ("squad-" if kind == "squad" else "domain-") + uuid.uuid4().hex[:10]
            if kind == "squad":
                used = {r["root"] for r in self.records() if r["kind"] == "squad"}
                slot = 1
                while True:
                    root = self.root/'instances'/f'slot-{slot:02d}'
                    if str(root) not in used and not root.exists() and not root.is_symlink():
                        root.mkdir(mode=0o700)
                        break
                    slot += 1
            else:
                root = self.private / "domains" / ident
                root.mkdir(parents=True, mode=0o700)
            reserved = {self.args.port, *(port for r in self.records() for port in r["ports"])}
            self.insert(ident, name.strip(), kind, root, available_ports(4 if kind == "squad" else 2, reserved), mode, description.strip())
        try:
            await self.start(ident)
            if kind == 'squad' and restore_members:
                await self.call(self.record(ident), 'settings', 'POST', {'restore_members': restore_members})
        except Problem:
            # Keep the registered resource and its data visible for explicit retry.
            raise
        return await self.detail(ident)

    async def rename(self, ident, body):
        async with self.locks.setdefault(ident, asyncio.Lock()):
            return await self._rename(ident, body)

    async def _rename(self, ident, body):
        record = self.record(ident)
        name = body.get("name", record['name'])
        description = body.get("description", record["description"])
        if 'restore_members' in body and (record['kind'] != 'squad' or type(body['restore_members']) is not bool):
            raise Problem('restore_members 须为 Squad 的布尔值设置')
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or not isinstance(description, str) or len(description) > 1000:
            raise Problem("名称须为 1–60 字，说明不超过 1000 字")
        name = name.strip()
        if self.db.execute("SELECT 1 FROM resources WHERE name=? AND id!=?", (name, ident)).fetchone():
            raise Problem("该名称已存在", 409)
        if self.live(ident):
            await self.call(record, "settings" if record["kind"] == "squad" else "domain", "POST",
                            {"name": name, **({'restore_members': body['restore_members']} if 'restore_members' in body else {})})
        elif 'restore_members' in body:
            from .store import Store
            store = Store(Path(record['root']) / 'control/squad.db')
            try:
                store.set_restore_members(body['restore_members'])
            finally:
                store.db.close()
        if 'restore_members' in body:
            self.cache.setdefault(ident, {})['restore_members'] = body['restore_members']
        with self.db:
            self.db.execute("UPDATE resources SET name=?,description=? WHERE id=?", (name, description, ident))
        return self.public(self.record(ident))

    async def relay_connections(self, selected="", browser=False):
        connections = []
        for record in self.records():
            if self.live(record["id"]):
                connections.append({"id": record["id"], "url": self.relay_web_url(record["id"]) if browser else self.relay_url(record), "label": record["name"],
                    "token": self.token_file(record, relay=True).read_text(encoding="utf-8").strip()})
        if not connections:
            raise Problem("请先启动一个 Squad 或 Relay 域", 409)
        target = next((c for c in connections if c["id"] == selected), connections[0])
        bundle = {"connections": connections, "selected": target["url"], "manager": self.web_origin()+"/squad" if browser else f"http://127.0.0.1:{self.args.port}"}
        landing = self.web_origin()+"/relay" if browser else connections[0]["url"]
        return {"url": landing + "/#connections=" + quote(json.dumps(bundle, ensure_ascii=False), safe="")}

    async def close(self):
        await asyncio.gather(*(self.stop_process(ident, timeout=None) for ident in list(self.processes)))
        self.db.close()


def make_app(fleet, token):
    from .browser_auth import BrowserAuth
    browser_auth = BrowserAuth(fleet.private / 'browser-auth.sqlite3', fleet.web_origin(), token)

    @web.middleware
    async def browser_headers(request, handler):
        response = await handler(request)
        if request.path.startswith('/squad/'):
            response.headers.update({'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
                'X-Content-Type-Options':'nosniff', 'Content-Security-Policy':"frame-ancestors 'none'"})
        return response

    @web.middleware
    async def auth(request, handler):
        is_api = request.path.startswith(("/api/", "/squad/api/"))
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("Origin"):
            allowed = {fleet.web_origin()}
            if request.host.split(":")[0] in {"127.0.0.1", "localhost"}:
                allowed.add("http://" + request.host)
            if request.headers["Origin"] not in allowed:
                raise Problem("请求来源不被允许", 403)
        if is_api:
            supplied = request.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, "Bearer " + token):
                if not request.path.startswith('/squad/api/') or not browser_auth.session(request):
                    raise Problem("需要管理入口的用户凭据", 401)
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    browser_auth.origin_check(request)
        response = await handler(request)
        if is_api or request.path.startswith("/relay/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app = web.Application(middlewares=[browser_headers, errors, auth], client_max_size=128 * 1024)
    app.router.add_get('/squad/auth/{action:status}', browser_auth.handler)
    app.router.add_post('/squad/auth/{action:.*}', browser_auth.handler)

    async def index(request):
        return web.FileResponse(Path(__file__).parent / "web/console.html")

    async def asset(request):
        return web.FileResponse(Path(__file__).parent / "web" / request.match_info["name"])

    async def state(request):
        data = await fleet.state()
        return web.json_response(fleet.browser_data(data) if request.path.startswith("/squad/") else data)

    async def create(request):
        body = await request.json()
        return web.json_response(await fleet.create("domain" if request.path.endswith("domains") else "squad", body["name"], body.get("description", ""), body.get("mode", "fake"), body.get('restore_members', False)))

    async def startup(request):
        if request.method == 'GET':
            return web.json_response(await fleet.startup.status())
        return web.json_response(await fleet.startup.configure((await request.json()).get('enabled')))

    async def action(request):
        ident, action = request.match_info["id"], request.match_info["action"]
        if action in {"start", "stop"}:
            result = await getattr(fleet, action)(ident)
        elif action == 'delete':
            result = await fleet.remove(ident)
        elif action == "settings":
            result = await fleet.rename(ident, await request.json())
        else:
            raise Problem("未知操作", 404)
        return web.json_response(result)

    async def detail(request):
        data = await fleet.detail(request.match_info["id"])
        return web.json_response(fleet.browser_data(data) if request.path.startswith("/squad/") else data)

    async def proxy(request):
        record = fleet.record(request.match_info.get("id", "studio"))
        if record["kind"] != "squad":
            raise Problem("这不是 Squad", 400)
        path = request.match_info["tail"]
        import re
        if not re.fullmatch(r"(?:agents|bulk|settings|relay-login|agents/[0-9a-f]{32}/(?:start|stop|configure|reconnect|terminal|archive|restore))", path):
            raise Problem("未知接口", 404)
        result = await fleet.call(record, path, request.method, await request.json() if request.can_read_body else None)
        return web.json_response(result)

    async def connections(request):
        return web.json_response(await fleet.relay_connections(request.query.get("selected", ""), browser=request.path.startswith("/squad/")))

    async def canonical(request):
        raise web.HTTPPermanentRedirect(request.path + "/")

    async def relay_index(request):
        import relay
        ident = request.match_info.get("id", "studio")
        fleet.record(ident)
        text = (Path(relay.__file__).parent / "web/index.html").read_text(encoding="utf-8")
        available = [{"url":fleet.relay_web_url(r["id"]),"label":r["name"]} for r in fleet.records() if fleet.live(r["id"])]
        metadata = ('<meta name="relay-domains" content="'+html.escape(json.dumps(available,ensure_ascii=False),quote=True)+'">'+ '<meta name="relay-default-url" content="'+html.escape(fleet.relay_web_url(ident),quote=True)+'">'
                    '<meta name="relay-manager-url" content="'+html.escape(fleet.web_origin()+"/squad",quote=True)+'">')
        text = text.replace('<title>', metadata+'<title>', 1)
        for filename in ('style.css', 'app.js', 'auth.js', 'app-shell.js', 'manifest.webmanifest', 'icon-192.png'):
            text = text.replace('./'+filename, '/relay/'+filename)
        return web.Response(text=text, content_type="text/html")

    async def relay_asset(request):
        import relay
        return web.FileResponse(Path(relay.__file__).parent / "web" / request.match_info["name"])

    async def relay_proxy(request):
        raw = request.headers.get("Authorization", "")
        path = request.match_info["tail"]
        relay_cookies = {'__Secure-relay_session','__Secure-relay_challenge','relay_session','relay_challenge'}
        anonymous = {'auth/status','auth/password/login','auth/passkeys/login/options','auth/passkeys/login/verify','auth/logout'}
        if path not in anonymous and not (raw.startswith('Bearer ') and raw[7:].strip()) and not any(request.cookies.get(k) for k in relay_cookies):
            raise Problem('请先登录这个 Relay 域',401)
        read_paths = {"state", "accounts", "messages", "receive", "wait", "auth/status", "groups"}
        file_upload = request.method == "POST" and path == "files"
        file_download = request.method == "GET" and bool(re.fullmatch(r"files/[0-9a-f]{32}", path))
        write_paths = {"accounts", "messages", "ack", "domain", "groups", "auth/password/login", "auth/password/set", "auth/logout", "auth/passkeys/login/options", "auth/passkeys/login/verify", "auth/passkeys/register/options", "auth/passkeys/register/verify", "auth/passkeys/remove"}
        allowed = file_upload or file_download or path in (read_paths if request.method == "GET" else write_paths) or (
            request.method == "POST" and re.fullmatch(r"accounts/[A-Za-z0-9_@-]{1,64}", path)) or bool(
            re.fullmatch(r"groups/[1-9][0-9]{0,17}(?:/messages)?", path))
        if not allowed:
            raise Problem("未知 Relay 接口", 404)
        record = fleet.record(request.match_info["id"])
        if not fleet.live(record["id"]):
            raise Problem("此聊天域已暂停", 409)
        # Forward only Relay's own domain-scoped browser cookies. Squad and
        # unrelated app cookies, forwarding headers and MCP never reach Relay.
        headers = {"Authorization":raw} if raw else {}
        cookies = {k:v for k,v in request.cookies.items() if k in {
            '__Secure-relay_session','__Secure-relay_challenge','relay_session','relay_challenge'}}
        if cookies:
            from http.cookies import SimpleCookie
            selected_cookies = SimpleCookie(cookies)
            headers['Cookie'] = selected_cookies.output(header='',sep=';').strip()
        if request.headers.get('Origin'):
            headers['Origin'] = request.headers['Origin']
        if request.can_read_body:
            headers["Content-Type"] = request.headers.get("Content-Type", "") if file_upload else "application/json"
        if file_upload:
            from relay.files import MAX_FILE_SIZE
            if request.content_length is not None and request.content_length > MAX_FILE_SIZE + 128 * 1024:
                raise Problem("单个文件最大 25 MiB", 413)
        if file_download:
            for name in ("Range", "If-Range"):
                if name in request.headers:
                    headers[name] = request.headers[name]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300 if file_upload or file_download else 40), auto_decompress=not file_download) as session:
            try:
                async with session.request(request.method, fleet.relay_url(record)+"/api/"+path,
                    params=request.query, data=(request.content.iter_chunked(64 * 1024) if file_upload else await request.read()) if request.can_read_body else None,
                    headers=headers, allow_redirects=False) as response:
                    if file_download:
                        result = web.StreamResponse(status=response.status, headers={
                            key: value for key, value in response.headers.items() if key.lower() in {
                                "content-type", "content-length", "content-disposition", "content-range",
                                "accept-ranges", "x-content-type-options", "content-security-policy"}})
                        result.headers["Cache-Control"] = "no-store"
                        await result.prepare(request)
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            await result.write(chunk)
                        await result.write_eof()
                        return result
                    result = web.Response(body=await response.read(), status=response.status,
                        headers={"Content-Type":response.headers.get("Content-Type","application/json"),"Cache-Control":"no-store"})
                    for cookie in response.headers.getall('Set-Cookie', []):
                        result.headers.add('Set-Cookie',cookie)
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise Problem("暂时无法连接聊天域", 502) from None

    app.router.add_get("/", index)
    app.router.add_get("/{name:console\\.(?:css|js)|app-shell\\.js|manifest\\.webmanifest|icon-(?:192|512)\\.png|sw\\.js}", asset)
    app.router.add_get("/api/state", state)
    app.router.add_get('/api/startup', startup)
    app.router.add_post('/api/startup', startup)
    app.router.add_post("/api/squads", create)
    app.router.add_post("/api/domains", create)
    app.router.add_post("/api/resources/{id}/{action}", action)
    app.router.add_get("/api/squads/{id}/state", detail)
    app.router.add_get("/api/relay-connections", connections)
    app.router.add_route("*", "/api/squads/{id}/{tail:.*}", proxy)
    # Existing local bookmarks and scripts continue to address the primary Squad.
    app.router.add_get("/api/relay-login", connections)
    app.router.add_route("*", "/api/{tail:agents(?:/.*)?}", proxy)
    app.router.add_get("/squad", canonical)
    app.router.add_get("/squad/", index)
    app.router.add_get("/squad/{name:console\\.(?:css|js)|app-shell\\.js|manifest\\.webmanifest|icon-(?:192|512)\\.png|sw\\.js}", asset)
    app.router.add_get("/squad/api/state", state)
    app.router.add_get('/squad/api/startup', startup)
    app.router.add_post('/squad/api/startup', startup)
    app.router.add_post("/squad/api/squads", create)
    app.router.add_post("/squad/api/domains", create)
    app.router.add_post("/squad/api/resources/{id}/{action}", action)
    app.router.add_get("/squad/api/squads/{id}/state", detail)
    app.router.add_get("/squad/api/relay-connections", connections)
    app.router.add_route("*", "/squad/api/squads/{id}/{tail:.*}", proxy)
    app.router.add_get("/relay", canonical)
    app.router.add_get("/relay/", relay_index)
    app.router.add_get("/relay/{name:auth\\.js|app\\.js|style\\.css|app-shell\\.js|manifest\\.webmanifest|icon-(?:192|512)\\.png|sw\\.js}", relay_asset)
    app.router.add_get("/relay/domains/{id}", canonical)
    app.router.add_get("/relay/domains/{id}/", relay_index)
    app.router.add_route("GET", "/relay/domains/{id}/api/{tail:.*}", relay_proxy)
    app.router.add_route("POST", "/relay/domains/{id}/api/{tail:.*}", relay_proxy)
    return app


async def serve(args):
    root = Path(args.root).expanduser().resolve()
    private = root / "control"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (private / "console.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("该管理入口已经在运行") from None
    token_file = private / "owner.token"
    if not token_file.exists():
        secret_file(token_file, secrets.token_urlsafe(32))
    fleet = Fleet(root, args)
    stop = asyncio.Event()
    if os.name == 'nt':
        from squad._windows import install_stop_handler
        install_stop_handler(asyncio.get_running_loop(), stop.set)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    runner = web.AppRunner(make_app(fleet, token_file.read_text(encoding="utf-8").strip()))
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        results = await asyncio.gather(*(fleet.start(r["id"]) for r in fleet.records() if r["enabled"]), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                print("服务启动问题:", str(result), flush=True)
        print(f"Squad Console: http://127.0.0.1:{args.port}  凭据文件: {token_file}", flush=True)
        await stop.wait()
    finally:
        await runner.cleanup()
        await fleet.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="Manage multiple independent Squads and Relay domains")
    parser.add_argument("--root", default=default_root())
    parser.add_argument('--startup-unit', default=os.environ.get('ASTER_SQUAD_STARTUP_UNIT', ''), help='Installed user systemd service controlled by the owner startup toggle')
    parser.add_argument('--desktop-root', default='', help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=19910)
    parser.add_argument("--primary-port", type=int, default=19912)
    parser.add_argument("--mcp-port", type=int, default=19911)
    parser.add_argument("--relay-port", type=int, default=19920)
    parser.add_argument("--relay-mcp-port", type=int, default=19921)
    parser.add_argument("--name", default="Aster Studio")
    parser.add_argument("--mode", choices=["fake", "real"], default="fake")
    parser.add_argument("--models", default=os.environ.get("SQUAD_MODEL", ""))
    parser.add_argument("--responses-url", default=os.environ.get("SQUAD_RESPONSES_URL", "https://api.openai.com/v1/responses"))
    parser.add_argument("--model-catalog", default=os.environ.get("SQUAD_MODEL_CATALOG", ""))
    parser.add_argument("--mcp-catalog", default=os.environ.get("SQUAD_MCP_CATALOG", ""))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--private-root", action="append", default=[])
    parser.add_argument("--public-url", default=os.environ.get("SQUAD_PUBLIC_URL", ""), help="明确配置的 HTTPS 网关 origin，例如 https://example.com:8443")
    args = parser.parse_args()
    if args.public_url:
        url = urlsplit(args.public_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment:
            parser.error("--public-url 必须为不带路径或凭据的 HTTPS origin")
        args.public_url = args.public_url.rstrip("/")
    ports = [args.port, args.primary_port, args.mcp_port, args.relay_port, args.relay_mcp_port]
    if len(set(ports)) != len(ports):
        parser.error("管理入口和默认 Squad 的端口不能重复")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
