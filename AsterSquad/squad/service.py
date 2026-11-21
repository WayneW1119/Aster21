from __future__ import annotations

import asyncio
import os
if os.name == "nt":
    from squad._windows import is_reparse, spawn as windows_spawn
import json
import re
import shutil
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
import toml
import tomllib

from relay.server import secret_file
from relay.store import Problem

from .store import Store
from .models import worker_model
from . import mcp_services, root_worker
from .telemetry import merge as merge_telemetry
from aster.permissions import access_for, covers, runtime_read_paths, gpu_devices

ASSETS = Path(__file__).parent / "assets"
MAX_AGENTS = 16
POLICY_WAIT_SECONDS = 5
ACCESS_LEVEL = {"deny": 0, "read": 1, "write": 2}


class AdminLaunchProblem(Problem):
    """UAC did not produce a connected administrator worker."""


def workspace_policy(workspace, grants):
    defaults = {str(workspace): {"path": str(workspace), "access": "write"}} if workspace else {}
    return list({**defaults, **{g["path"]: g for g in grants}}.values())


def can_adjust_grants(permitted, before, after):
    """Require authority over changed regions, preserving independent grants.

    Every policy is constant between ancestor/descendant boundaries. Unmatched
    paths are denied, just as they are in the execution sandbox.
    """
    boundaries = {r["path"] for r in [*permitted, *before, *after]}
    for path in boundaries:
        authority = ACCESS_LEVEL[access_for(path, permitted, default="deny")]
        old = ACCESS_LEVEL[access_for(path, before)]
        new = ACCESS_LEVEL[access_for(path, after)]
        if old != new and max(old, new) > authority:
            return False
    return True


def retains_delegated_grants(before, after, child):
    """Only a reduction of the parent's authority can invalidate a child grant."""
    boundaries = {r["path"] for r in [*before, *after, *child]}
    for path in boundaries:
        old, new, held = (ACCESS_LEVEL[access_for(path, rules, default="deny")]
                          for rules in (before, after, child))
        if new < old and held > new:
            return False
    return True


def privileged(config):
    """One owner-granted concept with platform-specific launchers."""
    return bool(config.get('run_as_root') or config.get('windows_admin'))


class Service:
    def __init__(self, root: Path, *, mode: str, relay_url: str, relay_mcp: str, squad_mcp: str,
                 owner_relay_token: str, domain: str, user_account: str, models: list[str], model_url: str, allow_http: bool = False, user_username: str = "user", private_roots: tuple[Path, ...] = (), model_catalog: dict | None = None, shared_skill_root: Path | None = None, mcp_catalog: dict | None = None):
        self.root, self.mode = root, mode
        self.shared_skill_root = (shared_skill_root or root).resolve()
        from .skills import install_builtins
        install_builtins(self.shared_skill_root)
        self.skills_root = root / "skills"
        self.skills_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for i in range(MAX_AGENTS):
            (root / "connections" / ("slot-%02d" % i)).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private = root / "control"
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode_file = self.private / "mode"
        if mode_file.exists() and mode_file.read_text(encoding="utf-8").strip() != mode:
            raise Problem("演示和真实模式不能共用数据目录，请使用新的 --root", 409)
        if not mode_file.exists():
            secret_file(mode_file, mode)
        self.store = Store(self.private / "squad.db")
        self.relay_url, self.relay_mcp, self.squad_mcp = relay_url, relay_mcp, squad_mcp
        self.owner_relay_token, self.domain, self.user_account = owner_relay_token, domain, user_account
        self.user_username = user_username
        self.private_roots = tuple(Path(p).resolve(strict=True) for p in private_roots)
        self.runtime_slots = [root / "runtime" / f"slot-{i:02d}" for i in range(MAX_AGENTS)]
        for folder in self.runtime_slots:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.models, self.model_url, self.allow_http = models, model_url, allow_http
        self.model_catalog = model_catalog or {}
        self.mcp_catalog = mcp_catalog or {}
        self.mcp_catalog_path = None
        self.auth_paths = tuple(Path(route['auth_home']) / 'credentials'
                                for route in self.model_catalog.values() if route.get('auth_home'))
        self.workers = {}
        self.pumps = {}
        self.locks = {}
        self.creation_lock = asyncio.Lock()
        self.inflight = {}
        self.policy_waiters = {}
        self.worker_capabilities = {}
        self.configuration_reservations = set()
        self.closing = False
        self.slots = [root / "workspaces" / f"slot-{i:02d}" for i in range(MAX_AGENTS)]
        # Fixed partitions exist before any sandbox starts: later children cannot
        # introduce a new unmasked credential directory to an existing worker.
        for folder in self.slots:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.startup_candidates = []
        for record in self.store.agents():
            if self.should_restore(record):
                self.startup_candidates.append(record["id"])
            if not record["archived"] and record["status"] not in {"offline", "error"}:
                self.store.update(record["id"], status="offline", error="宿主已重启；请显式启动以恢复原 Thread")

    def should_restore(self, record):
        policy = self.store.restore_members()
        enabled = record['config'].get('auto_start', False) if policy is None else policy
        return (enabled is True and record["run_requested"]
                and not record["archived"] and record["status"] != "error")

    async def restore_on_startup(self):
        # One pass per controller lifetime; failures remain visible, never retried here.
        candidates, self.startup_candidates = self.startup_candidates, []
        semaphore = asyncio.Semaphore(2)

        async def restore(ident):
            async with semaphore, self.lock(ident):
                if self.closing or not self.should_restore(self.store.agent(ident)) or ident in self.workers:
                    return
                self.store.audit("system", "auto_start_attempt", ident)
                try:
                    await self._start(ident)
                except Exception as error:
                    fields = {"error": "自动恢复失败；请检查后手动启动：" + str(error)}
                    if ident not in self.workers:
                        fields["status"] = "error"
                    self.store.update(ident, **fields)
                    self.store.audit("system", "auto_start_failed", ident)

        await asyncio.gather(*(restore(ident) for ident in candidates))

    def lock(self, ident):
        return self.locks.setdefault(ident, asyncio.Lock())

    def workspace(self, record):
        return self.slots[record["slot"]]

    def skill_sources(self, record):
        from .skills import sources
        return sources(self.root, self.shared_skill_root, record.get("id"))

    def connection_paths(self, record):
        folder = self.root / "connections" / ("slot-%02d" % record["slot"])
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder / "relay.json", folder / "watch.py"

    def credentials(self, ident):
        return self.private / "agents" / ident / "credentials.json"

    def permission_update(self, ident):
        path = self.credentials(ident).parent / "permission-update.json"
        if not path.exists():
            return None
        update = json.loads(path.read_text(encoding="utf-8"))
        if 'previous' in update:
            update['previous'] = Store.normalize_config(update['previous'])
        return update

    def permission_pending(self, ident):
        if ident in self.configuration_reservations:
            return True
        update = self.permission_update(ident)
        return bool(update and update["status"] == "pending")

    def save_permission_update(self, ident, update):
        path = self.credentials(ident).parent / "permission-update.json"
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8" if os.name == "nt" else None) as stream:
                json.dump(update, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def complete_permission_update(self, ident, request_id, ok, details):
        update = self.permission_update(ident)
        if not update or update["id"] != request_id or update["status"] != "pending":
            return
        result = {"ok": ok, "details": details}
        try:
            if ok is None:
                update["error"] = "运行结果尚不确定；配置已保存，恢复运行时重新核对：" + details.get("error", "")
                result["pending"] = True
            else:
                if not ok:
                    config = dict(self.store.agent(ident)["config"])
                    keys = ("mcp_services", "allow_connect") if update.get("kind") == "mcp" else ("grants", "sandbox_enabled", "gpu_access", "local_ipc")
                    for key in keys:
                        if key in update["previous"]:
                            config[key] = update["previous"][key]
                        else:
                            config.pop(key, None)
                    self.store.update(ident, config=config)
                self.prepare(self.store.agent(ident))
                update.update(status="applied" if ok else "failed", details=details, completed=time.time())
            self.save_permission_update(ident, update)
            if ok is not None:
                self.store.audit("system", "permissions_applied" if ok else "permissions_failed", ident)
        except Exception as error:
            # Never lose ownership of the live worker because a receipt could
            # not be persisted. The durable desired config remains authoritative
            # on resume, and delegation stays blocked by the pending journal.
            result = {"pending": True, "details": {"error": "权限结果持久化失败：" + str(error)}}
        if waiter := self.policy_waiters.pop(request_id, None):
            if not waiter.done():
                waiter.set_result(result)

    async def apply_live_permissions(self, record, update):
        ident, request_id = record["id"], update["id"]
        waiter = asyncio.get_running_loop().create_future()
        self.policy_waiters[request_id] = waiter
        if update.get("kind") == "mcp":
            config_file = self.credentials(ident).with_name("worker.json")
            worker = json.loads(config_file.read_text(encoding="utf-8"))
            command = ({"op": "suites", "installations": worker["suites"], "grants": worker["suite_grants"], "allow_connect": worker["allow_connect"]} if "suites" in worker else
                       {"op": "mcp_servers", "servers": worker["mcp_servers"]})
        else:
            policy = {"rules": self.rules(record)} if self.sandbox_enabled(record) else None
            if policy is not None:
                for key, source in (('gpu', 'gpu_access'), ('local_ipc', 'local_ipc')):
                    if record['config'].get(source):
                        policy[key] = True
            command = {"op": "filesystem_policy", "policy": policy}
        try:
            proc = self.workers[ident]
            proc.stdin.write((json.dumps({**command, "id": request_id}) + "\n").encode())
            await proc.stdin.drain()
        except (KeyError, BrokenPipeError, ConnectionResetError):
            # The delivery may be uncertain. Keep the authorized desired config
            # and journal; next start applies it before exposing a ready worker.
            update["error"] = "运行时已退出；权限配置已保存，将在恢复运行时应用"
            self.save_permission_update(ident, update)
            self.policy_waiters.pop(request_id, None)
            return {**self.view(self.store.agent(ident)), "configuration_update": "pending"}
        try:
            result = await asyncio.wait_for(asyncio.shield(waiter), POLICY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return {**self.view(self.store.agent(ident)), "configuration_update": "pending"}
        if result.get("pending"):
            view = self.view(self.store.agent(ident))
            if view["permission_update"]:
                view["permission_update"]["error"] = result["details"].get("error")
            return {**view, "configuration_update": "pending"}
        if not result["ok"]:
            raise Problem("权限更新未生效，原权限保留：" + result["details"].get("error", "运行时拒绝候选策略"), 409)
        return {**self.view(self.store.agent(ident)), "configuration_update": "live"}

    def can_manage(self, actor, ident):
        record = self.store.agent(ident)
        if actor != "owner" and record["parent"] != actor:
            raise Problem("只能管理自己的直接 child", 403)
        return record

    @staticmethod
    def can_delegate(path, access, permitted):
        return covers(permitted, [{"path": str(path), "access": access}])

    def delegation_policy(self, actor):
        parent = self.store.agent(actor)
        return workspace_policy(self.workspace(parent), parent["config"]["grants"])

    def grants(self, actor, grants, *, workspace=None, previous=None):
        if not isinstance(grants, list) or len(grants) > 24:
            raise Problem("grants 须为最多 24 条路径规则")
        permitted = []
        if actor != "owner":
            if self.permission_pending(actor):
                raise Problem("成员权限正在更新，请等待生效后再转授路径", 409)
            permitted = self.delegation_policy(actor)
        normalized = []
        for entry in grants:
            if (not isinstance(entry, dict) or set(entry) != {"path", "access"}
                    or not isinstance(entry["access"], str) or entry["access"] not in {"read", "write", "deny"}
                    or not isinstance(entry["path"], str) or not entry["path"].strip()
                    or "\0" in entry["path"] or len(entry["path"]) > 4096):
                raise Problem("路径规则须包含绝对路径 path 和 read/write/deny access")
            # '/' is the cross-volume root rule on Windows, not a CWD-relative path.
            if os.name == 'nt' and entry['path'] == '/':
                if any(e['path'] == '/' for e in normalized):
                    raise Problem('路径规则重复')
                normalized.append(dict(entry))
                continue
            original = Path(entry["path"])
            if os.name == 'nt' and any(is_reparse(p) for p in (original,*original.parents)):
                raise Problem('Windows grants must not cross reparse points')
            if not original.is_absolute() or ".." in original.parts or original.is_symlink() or any(p.is_symlink() for p in original.parents):
                raise Problem("路径须为不经过符号链接、不含 .. 的绝对路径")
            if not original.exists() and entry["access"] != "write":
                raise Problem("只读和禁止访问的路径须已存在；新目录请添加到可读写")
            if any(p.exists() and not p.is_dir() for p in original.parents):
                raise Problem("路径的父级必须是目录")
            path = original.resolve()
            # No grants can intersect the controller or its ancestors; workspace
            # delegation is only permitted from the immediate parent workspace.
            protected = [self.private, self.skills_root, self.shared_skill_root / "skills", self.root / "connections", self.root / "runtime", *self.private_roots, *self.auth_paths,
                         *[p for p in self.slots if p != workspace and
                           (actor == "owner" or p != self.workspace(self.store.agent(actor)))]]
            # The shared data tree is already masked by default_denied, including
            # future spaces. Also prevent explicit grants from reopening peers.
            if self.root == self.shared_skill_root or not path.is_relative_to(self.root):
                protected.append(self.shared_skill_root/'instances')
            if path != Path('/') and entry["access"] != "deny" and any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
                raise Problem("不能授权平台私有路径、其他 workspace 或包含它们的目录", 403)
            legacy_private = [p / name for p in self.slots if p != workspace
                              for name in (".connections", ".relay") if (p / name).exists()]
            if entry["access"] != "deny" and any(path.is_relative_to(p) for p in legacy_private):
                raise Problem("不能授权其他成员的旧版连接凭据目录", 403)
            if entry["access"] == "deny" and workspace and workspace == path:
                raise Problem("不能禁止访问自己的工作区本身；可以限制工作区内的子路径")
            if any(e["path"] == str(path) for e in normalized):
                raise Problem("路径规则重复")
            normalized.append({"path": str(path), "access": entry["access"]})
        if actor != "owner":
            if previous is None:
                if not covers(permitted, normalized):
                    raise Problem("不能转授自己没有的路径权限；宽目录授权须保留其中的限制", 403)
            else:
                if not can_adjust_grants(permitted, workspace_policy(workspace, previous), workspace_policy(workspace, normalized)):
                    raise Problem("只能调整自己权限范围内的路径权限；范围之外的已有权限须保持不变", 403)
                if any(g["access"] == "write" and not Path(g["path"]).exists()
                       and access_for(g["path"], permitted, default="deny") != "write" for g in normalized):
                    raise Problem("不能在自己的权限范围之外准备缺失目录，请由用户或对应成员恢复路径", 403)
        return normalized

    @staticmethod
    def materialize_grants(grants):
        """Create missing write directories without following symlink components."""
        if os.name == 'nt':
            for grant in grants:
                if grant['access'] != 'write' or grant['path'] == '/':continue
                path=Path(grant['path'])
                if any(p.is_symlink() or is_reparse(p) for p in (path,*path.parents)):
                    raise Problem('Windows write paths must not cross reparse points')
                if not path.exists():path.mkdir(parents=True)
            return
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        for grant in grants:
            if grant["access"] != "write":
                continue
            path = Path(grant["path"])
            fd = None
            try:
                fd = os.open("/", flags)
                for index, part in enumerate(path.parts[1:]):
                    last = index == len(path.parts) - 2
                    try:
                        child = os.open(part, flags if not last else os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    except FileNotFoundError:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                        child = os.open(part, flags, dir_fd=fd)
                    if last:
                        if not (stat.S_ISDIR(os.fstat(child).st_mode) or stat.S_ISREG(os.fstat(child).st_mode)):
                            os.close(child)
                            raise OSError("Path must be a regular file or directory")
                    os.close(fd)
                    fd = child
            except OSError:
                raise Problem("无法准备可写路径，请检查宿主目录权限及路径是否经过符号链接：" + str(path)) from None
            finally:
                if fd is not None:
                    os.close(fd)

    def default_denied(self, record):
        # Hide data trees once, then expose only this member's required roots.
        base = sorted(set([self.shared_skill_root, self.root, *self.private_roots, *self.auth_paths]),
                      key=lambda p: (len(p.parts), str(p)))
        denied = [p for p in base if not any(p != q and p.is_relative_to(q) for q in base)]
        grants = record["config"]["grants"]
        for source in self.skill_sources(record):
            path = Path(source["path"])
            if any(path.is_relative_to(p) for p in base):
                continue
            if not self.can_delegate(path, "write", grants):
                denied.append(path)
        # Old connection files inside a shared parent workspace remain private,
        # even when the whole parent workspace has been explicitly shared.
        for folder in self.slots:
            if folder != self.workspace(record) and any(
                    g["access"] != 'deny' and Path(g['path']).is_relative_to(folder) for g in grants):
                denied.extend(p for p in (folder / '.connections', folder / '.relay') if p.exists())
        return list(dict.fromkeys(denied))

    def platform_openings(self, record):
        connection, _ = self.connection_paths(record)
        return [{"path": str(self.workspace(record)), "access": "write"},
                {"path": str(connection.parent), "access": "read"},
                *([{"path": str(self.aster_home(record)), "access": "read"}] if self.mode == 'real' else [])]

    def rules(self, record):
        combined = {g['path']: g for g in self.platform_openings(record)}
        combined.update({g['path']: g for g in record['config']['grants']})
        combined.update({str(p): {'path': str(p), 'access': 'deny'} for p in self.default_denied(record)})
        result = list(combined.values())
        if len(result) > 65 or len(result) == 65 and not any(g['path'] == '/' for g in result):
            raise Problem("沙箱规则总数超过 64，请减少额外路径授权")
        return result

    def aster_home(self, record):
        # Native program state is mounted by Aster into a synthetic sandbox path.
        # It must not sit beneath an explicit deny covering controller secrets.
        return self.private / "aster" if self.mode == "fake" else self.runtime_slots[record["slot"]]

    def sandbox_enabled(self, record):
        return self.mode == "real" and record["config"].get("sandbox_enabled", True) and not privileged(record["config"])

    async def release_root_runtime(self, record):
        if os.name == "nt":
            return
        path = self.credentials(record['id']).parent / 'worker.json'
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get('run_as_root'):
            await root_worker.release(path)

    def instruction(self, record):
        values = {"id": record["id"], "handle": record["handle"], "workspace": str(self.workspace(record)),
                  "parent": record["parent"] or "无", "relay_url": self.relay_url, "username": record["handle"],
                  "user_username": self.user_username, "identity": record["config"]["instruction"],
                  "relay_connection": str(self.connection_paths(record)[0]),
                  "relay_watch": str(self.connection_paths(record)[1])}
        text = (ASSETS / "instruction.md").read_text(encoding="utf-8")
        alias = record["config"].get("relay_namespace")
        values["relay_endpoint"] = "默认 Relay 连接：" + alias if alias else "Relay 服务：" + self.relay_url
        values["watch_guidance"] = "核对默认收件监听" if alias else "检查、建立消息监听"
        return re.sub(r"\{\{(\w+)\}\}", lambda hit: str(values[hit[1]]), text)

    async def relay(self, token, method, path, body=None):
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + token}) as session:
            async with session.request(method, self.relay_url + path, json=body, timeout=aiohttp.ClientTimeout(total=15)) as response:
                result = await response.json()
                if response.status >= 400:
                    raise Problem(result.get("error", "Relay 请求失败"), response.status)
                return result

    def service_ids(self, record):
        configured = record["config"].get("mcp_services")
        if configured is not None:
            return sorted(name for name in configured if name in self.mcp_catalog)
        return mcp_services.selection(self.mcp_catalog)

    def refresh_mcp_catalog(self):
        """Discover new opt-in entries without changing running grants."""
        if not self.mcp_catalog_path:
            return
        try:
            candidate = mcp_services.load_catalog(self.mcp_catalog_path)
        except (OSError, ValueError):
            return
        additions = {name: entry for name, entry in candidate.items()
                     if name not in self.mcp_catalog and not entry["default"]}
        if len(self.mcp_catalog) + len(additions) <= mcp_services.MAX_SERVICES:
            self.mcp_catalog.update(additions)

    def mcp_servers(self, record):
        return {"squad": {"url": self.squad_mcp, "bearer_token_env_var": "SQUAD_TOKEN", "required": True},
                "relay": {"url": self.relay_mcp, "bearer_token_env_var": "RELAY_TOKEN", "required": True},
                **{name: self.mcp_catalog[name]["server"] for name in self.service_ids(record)}}

    def delegable_permissions(self, config):
        """Explicit capabilities, independent of incidental root/sandbox bypasses."""
        result = {key for key in ('gpu_access', 'host_systemd', 'local_ipc', 'allow_connect') if config.get(key, False)}
        if not config.get('sandbox_enabled', True):
            result.add('sandbox_disabled')
        result.update('mcp:' + name for name in self.service_ids({'config': config}))
        return result

    def validate_suite_permissions(self, config):
        from .suites import requirements
        if (config.get("relay_namespace") and self.sandbox_enabled({"config": config})
                and not config.get("local_ipc") and "browser" in self.service_ids({"config": config})
                and "local_ipc" in requirements("browser", self.mcp_catalog["browser"]["server"])):
            raise Problem("浏览器套件需要本机进程通信权限，请授权或关闭浏览器套件", 409)

    def check_permission_delegation(self, actor, record, config):
        before = self.delegable_permissions(record['config']) if record else set()
        after = self.delegable_permissions(config)
        changed = before ^ after
        if actor != 'owner' and changed:
            if self.permission_pending(actor):
                raise Problem('上级权限正在更新，请等待生效后再转授', 409)
            permitted = self.delegable_permissions(self.store.agent(actor)['config'])
            if changed - permitted:
                raise Problem('只能调整自己拥有的权限；范围外的已有授权须保持不变：' + ', '.join(sorted(changed - permitted)), 403)
        if not record or not changed:
            return
        if record['parent'] and self.permission_pending(record['parent']):
            raise Problem('上级权限正在更新，请等待生效后再修改下级授权', 409)
        removed = before - after
        if removed:
            for child in self.store.agents():
                if child['parent'] != record['id']:
                    continue
                if self.permission_pending(child['id']):
                    raise Problem('下级权限正在更新，请等待生效后再收窄上级权限', 409)
                retained = removed & self.delegable_permissions(child['config'])
                if retained:
                    raise Problem('下级 ' + child['handle'] + ' 仍持有授权，请先收回下级权限：' + ', '.join(sorted(retained)), 409)

    def set_gpu_permission(self, config, value):
        if value is None:
            return
        if type(value) is not bool:
            raise Problem('gpu_access 须为布尔值')
        if value:
            if self.mode != 'real' or not gpu_devices():
                raise Problem('当前宿主不支持沙箱 GPU 设备授权；目前支持 WSL2 /dev/dxg', 409)
            config['gpu_access'] = True
        else:
            config.pop('gpu_access', None)

    def set_local_ipc(self, config, value):
        if value is None:
            return
        if type(value) is not bool:
            raise Problem('local_ipc 须为布尔值')
        if value and (not sys.platform.startswith('linux') or os.name == 'nt' or self.mode != 'real'):
            raise Problem('本机进程通信授权目前仅支持 Linux / WSL 真实空间', 409)
        if value:
            config['local_ipc'] = True
        else:
            config.pop('local_ipc', None)

    async def create(self, actor, handle, instruction, model="", grants=None, parent=None, sandbox_enabled=True, mcp_services_ids=None, run_as_root=False, *, gpu_access=False, local_ipc=False, allow_connect=False, host_systemd=False, windows_admin=None, auto_start=False):
        async with self.creation_lock:
            if self.closing:
                raise Problem("空间正在关闭，不能创建新成员", 409)
            if type(sandbox_enabled) is not bool:
                raise Problem("sandbox_enabled 须为布尔值")
            if type(local_ipc) is not bool:
                raise Problem('local_ipc 须为布尔值')
            if type(auto_start) is not bool:
                raise Problem("auto_start 须为布尔值")
            if type(run_as_root) is not bool:
                raise Problem('run_as_root 须为布尔值')
            if run_as_root:
                if os.name == "nt":
                    raise Problem("root 身份只适用于 Linux，请使用 Windows 管理员模式", 409)
                if actor != 'owner' or self.mode != 'real':
                    raise Problem('只有用户可以在真实空间授予 root 身份', 403)
                await root_worker.check(self.root)
            if actor != "owner" and parent is not None:
                raise Problem("agent 只能创建自己的直接 child", 403)
            parent = actor if actor != "owner" else parent
            if parent is not None and self.store.agent(parent)["archived"]:
                raise Problem("不能在已归档成员下创建 child", 409)
            if not isinstance(handle, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", handle) or handle == "user":
                raise Problem("handle 须为 1–24 位小写字母、数字、下划线或减号，以字母开头；user 保留")
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 20000:
                raise Problem("请提供 1–20000 字的身份与职责")
            if any(a["handle"] == handle for a in self.store.agents()):
                raise Problem("handle 已存在", 409)
            used = {a["slot"] for a in self.store.agents()}
            slot = next((i for i in range(MAX_AGENTS) if i not in used), None)
            if slot is None:
                raise Problem("第一版最多 16 个 agent", 409)
            model = model or self.models[0]
            if model not in self.models:
                raise Problem("模型不在宿主 allowlist 中", 403)
            if grants is None:
                # New members start private. The UI may explicitly offer / read;
                # children inherit it only when their parent actually has it.
                grants = []
            config = {"instruction": instruction, "model": model, "sandbox_enabled": sandbox_enabled, "run_as_root": run_as_root, "auto_start": auto_start,
                      "grants": self.grants(parent or "owner", grants, workspace=self.slots[slot])}
            from .suites import relay_namespace
            if type(allow_connect) is not bool:
                raise Problem("allow_connect 须为布尔值")
            config["allow_connect"] = allow_connect
            self.set_windows_admin(actor, config, windows_admin)
            self.set_local_ipc(config, local_ipc)
            if gpu_access:
                self.set_gpu_permission(config, gpu_access)
            elif type(gpu_access) is not bool:
                raise Problem('gpu_access 须为布尔值')
            self.refresh_mcp_catalog()
            config["relay_namespace"] = relay_namespace(getattr(self, "domain_name", self.domain), handle, self.mcp_catalog)
            if self.mcp_catalog or mcp_services_ids is not None:
                chosen = mcp_services.selection(self.mcp_catalog, mcp_services_ids)
                if actor != "owner" and mcp_services_ids is None:
                    chosen = [name for name in chosen if name in self.service_ids(self.store.agent(actor))]
                config["mcp_services"] = chosen
            if type(host_systemd) is not bool:
                raise Problem('host_systemd 须为布尔值')
            if host_systemd:
                config['host_systemd'] = True
            self.validate_suite_permissions(config)
            self.check_permission_delegation(actor, None, config)
            self.rules({"slot": slot, "config": config})
            self.materialize_grants(config["grants"])
            token = self.owner_relay_token if parent is None else json.loads(self.credentials(parent).read_text(encoding="utf-8"))["relay"]
            account = await self.relay(token, "POST", "/api/accounts", {"username": handle, "name": handle, "can_create": True})
            record, squad_token = self.store.create(handle, parent, slot, config, account["account"]["id"])
            secret_file(self.credentials(record["id"]), json.dumps({"squad": squad_token, "relay": account["token"]}))
            self.prepare(record)
            self.store.audit(actor, "create", record["id"])
        try:
            await self.start(actor, record["id"])
        except AdminLaunchProblem as error:
            return {**self.view(self.store.agent(record["id"])), "startup_warning": str(error)}
        return self.view(self.store.agent(record["id"]))

    def prepare(self, record):
        self.materialize_grants(record["config"]["grants"])
        workspace = self.workspace(record)
        credentials = json.loads(self.credentials(record["id"]).read_text(encoding="utf-8"))
        folder = self.credentials(record["id"]).parent
        instruction_file = folder / "instructions.md"
        secret_file(instruction_file, self.instruction(record))
        connection, watch = self.connection_paths(record)
        secret_file(connection, json.dumps({"url": self.relay_url, "domain": self.domain, "account": record["relay_account"], "username": record["handle"], "token": credentials["relay"], "mcp_url": self.relay_mcp}))
        shutil.copyfile(ASSETS / "relay_watch.py", watch)
        config = {"id": record["id"], "handle": record["handle"], "workspace": str(workspace), "home": str(self.aster_home(record)),
                  "thread": record["thread"], "mode": self.mode, "model": record["config"]["model"], "model_url": self.model_url,
                  "allow_http": self.allow_http, "rules": self.rules(record), "sandbox_enabled": self.sandbox_enabled(record),
                  "run_as_root": record['config'].get('run_as_root', False),
                  "user_account": self.user_account, "user_username": self.user_username,
                  "instructions": str(instruction_file), "squad_mcp": self.squad_mcp, "relay_mcp": self.relay_mcp,
                  "skills": self.skill_sources(record), "relay_connection": str(connection), "relay_watch": str(watch)}
        if record['config'].get('windows_admin'):
            config['windows_admin'] = True
        for key in ('gpu_access', 'local_ipc'):
            if record['config'].get(key):
                config[key] = True
        config["mcp_servers"] = mcp_services.clean(self.mcp_servers(record))
        if alias := record["config"].get("relay_namespace"):
            from .suites import installations
            config["allow_connect"] = record["config"].get("allow_connect", False)
            config["relay_namespace"] = alias
            config["suite_grants"] = (["local_ipc"] if config.get("local_ipc") or not config["sandbox_enabled"] else [])
            config["suites"] = installations(config, self.mcp_catalog, self.service_ids(record),
                                              self.shared_skill_root / "skills/global")
            config["mcp_servers"] = {}
        route = self.model_catalog.get(record["config"]["model"])
        if self.mode == "real" and route:
            config["model_config"] = worker_model(route)
        config_file = folder / "worker.json"
        previous_managed = json.loads(config_file.read_text(encoding="utf-8")).get("managed_mcp_services", []) if config_file.exists() else []
        config["managed_mcp_services"] = list(self.mcp_catalog)
        if record["thread"]:
            thread = self.aster_home(record) / "threads" / record["thread"]
            # Public SDK contract: edit these files while paused, then resume.
            path = thread / "thread.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            data["model"]["name"] = record["config"]["model"] if self.mode == "real" else ""
            if self.mode == "real":
                data["model"] = config.get("model_config") or {
                    "name": record["config"]["model"], "url": self.model_url,
                    "auth": "api_key", "api_key_env": "SQUAD_MODEL_KEY", "allow_http": self.allow_http}
            data["skills"] = {"workspace": False, "directories": [], "sources": config["skills"]}
            if "suites" not in config:
                mcp_file = thread / "mcp.toml"
                saved = tomllib.loads(mcp_file.read_text(encoding="utf-8")).get("mcp_servers", {}) if mcp_file.exists() else {}
                managed = {"squad", "relay", *previous_managed, *self.mcp_catalog, *record["config"].get("mcp_services", [])}
                config["mcp_servers"] = {**{name: value for name, value in saved.items() if name not in managed}, **config["mcp_servers"]}
                secret_file(mcp_file, toml.dumps({"mcp_servers": config["mcp_servers"]}))
                data["mcp"] = {"config": "mcp.toml"}
            # Harness disabled profiles require empty rules. Squad retains the
            # saved grants and derives the same protected rules on re-enable.
            data["sandbox"] = {"enabled": config["sandbox_enabled"], "format": 2, "rules": config["rules"] if config["sandbox_enabled"] else []}
            if config['sandbox_enabled']:
                for key, source in (('gpu', 'gpu_access'), ('local_ipc', 'local_ipc')):
                    if config.get(source):
                        data['sandbox'][key] = True
            secret_file(path, toml.dumps(data))
            secret_file(thread / "instructions.md", self.instruction(record))
        secret_file(config_file, json.dumps(config))
        return config_file, credentials

    async def start(self, actor, ident):
        self.can_manage(actor, ident)
        async with self.lock(ident):
            if actor != "owner" and privileged(self.store.agent(ident)["config"]):
                raise Problem("只有用户可以启动管理员成员", 403)
            self.store.update(ident, run_requested=1)
            return await self._start(ident)

    async def _start(self, ident):
        if self.closing:
            raise Problem("空间正在关闭，不能启动成员", 409)
        if ident in self.workers:
            if self.store.agent(ident)["status"] == "stopping":
                raise Problem("成员仍在安全暂停，请等待完成后再启动", 409)
            return {"ok": True, "status": self.store.agent(ident)["status"]}
        record = self.store.agent(ident)
        if record["archived"]:
            raise Problem("请先恢复已归档成员", 409)
        if record["config"]["model"] not in self.models:
            raise Problem("当前模型已不在宿主 allowlist 中，请先修改配置", 403)
        await self.release_root_runtime(record)
        if record['config'].get('run_as_root'):
            await root_worker.check(self.root)
        config_file, credentials = self.prepare(record)
        # Do not inherit controller or unrelated service credentials.
        allowed = ("PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "HOME", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if os.name == 'nt':
            allowed += ('SYSTEMROOT','SystemRoot','WINDIR','COMSPEC','PATHEXT','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY')
            allowed += ("ASTER_MXC_EXECUTOR", "ASTER_MXC_POWERSHELL")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        if os.name == 'nt':
            env['PYTHONIOENCODING'] = 'utf-8'
        env.update(SQUAD_TOKEN=credentials["squad"], RELAY_TOKEN=credentials["relay"],
                   SQUAD_MODEL_KEY=os.environ.get("SQUAD_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
        extras = {name: self.mcp_catalog[name]["server"] for name in self.service_ids(record)}
        for key in mcp_services.environment_keys(extras):
            if key in os.environ:
                env[key] = os.environ[key]
        route = self.model_catalog.get(record["config"]["model"])
        if self.mode == "real" and route:
            if route["auth"] == "chatgpt":
                env.pop("SQUAD_MODEL_KEY", None)
            else:
                key = os.environ.get(route["api_key_env"], "")
                if not key:
                    raise Problem("所选模型的宿主 API 凭据尚未配置", 409)
                env["SQUAD_MODEL_KEY"] = key
        log_path = self.private / "agents" / ident / "worker.log"
        if record["config"].get("windows_admin") is True:
            if os.name != "nt" or self.mode != "real":
                raise Problem("管理员模式只适用于 Windows 真实成员", 409)
            from .windows_admin import spawn_admin
            try:
                proc = await spawn_admin(config_file, env)
            except Exception as error:
                code = getattr(error, "winerror", None)
                message = "UAC 已取消，成员未启动" if code == 1223 else "无法启动管理员成员，请检查 Windows UAC"
                raise AdminLaunchProblem(message, 409) from error
        else:
            with log_path.open("ab") as log:
                command = root_worker.command(config_file) if record['config'].get('run_as_root') else [sys.executable, '-P', '-m', 'squad.worker', '--config', str(config_file)]
                proc = await (windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(*command,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log, env=env,
                    limit=256 * 1024)
        self.workers[ident] = proc
        self.inflight[ident] = set()
        self.store.update(ident, status="starting", error=None)
        ready = asyncio.get_running_loop().create_future()
        self.pumps[ident] = asyncio.create_task(self.pump(ident, proc, ready))
        if record['config'].get('run_as_root'):
            proc.stdin.write((json.dumps(env)+'\n').encode())
            await proc.stdin.drain()
        try:
            await asyncio.wait_for(asyncio.shield(ready), 30)
        except (asyncio.TimeoutError, Problem):
            await self._stop(ident)
            raise Problem(self.store.agent(ident)["error"] or "agent 启动超时；原 Thread 已保留", 409)
        self.store.audit("system", "start", ident)
        return {"ok": True, "status": "online"}

    async def pump(self, ident, proc, ready):
        try:
            while line := await proc.stdout.readline():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "thread":
                    self.store.update(ident, thread=event["thread"])
                elif kind == "ready":
                    self.worker_capabilities[ident] = event.get("capabilities", {})
                    if update := self.permission_update(ident):
                        if update["status"] == "pending":
                            self.complete_permission_update(ident, update["id"], True,
                                {"applied_on_resume": True, **event.get("mcp_status", {})})
                    self.store.update(ident, status="online")
                    await self.flush(ident)
                    if not ready.done():
                        ready.set_result(True)
                elif kind == "accepted":
                    self.store.accept(ident, event["id"])
                    self.inflight.get(ident, set()).discard(event["id"])
                elif kind == "runtime":
                    record = self.store.agent(ident)
                    self.store.update(ident, telemetry=merge_telemetry(record["telemetry"], event["data"], event["model"]))
                elif kind == "terminal":
                    self.store.terminal(ident, "out", event["text"])
                elif kind == "policy_queued":
                    update = self.permission_update(ident)
                    if update and update["id"] == event["id"] and update["status"] == "pending":
                        update["native_id"] = event["native_id"]
                        try:
                            self.save_permission_update(ident, update)
                        except OSError:
                            pass  # Correlation also lives in the worker; final receipt still resolves it.
                elif kind in {"policy_result", "mcp_result"}:
                    self.complete_permission_update(ident, event["id"], event["ok"], event["details"])
                elif kind == "error":
                    self.store.update(ident, status="error", error=event["error"])
            await proc.wait()
            if proc.returncode and not self.store.agent(ident)["error"]:
                self.store.update(ident, error=f"worker 异常退出 ({proc.returncode})；请查看 worker.log")
        except Exception as error:
            self.store.update(ident, error=str(error))
        finally:
            if self.workers.get(ident) is proc:
                self.workers.pop(ident, None)
                record = self.store.agent(ident)
                self.store.update(ident, status="error" if record["error"] else "offline")
            if not ready.done():
                ready.set_exception(Problem(self.store.agent(ident)["error"] or "worker 未就绪即退出", 409))
            self.store.audit("system", "exit", ident)

    async def stop(self, actor, ident):
        self.can_manage(actor, ident)
        async with self.lock(ident):
            self.store.update(ident, run_requested=0)
            await self._stop(ident)
        self.store.audit(actor, "stop", ident)
        return {"ok": True}

    async def _stop(self, ident, *, timeout=30):
        proc = self.workers.get(ident)
        if not proc:
            return
        self.store.update(ident, status="stopping")
        try:
            proc.stdin.write(b'{"op":"stop"}\n')
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            # A request/tool may still be running. Killing it loses the final
            # checkpoint and can leave an unknowable business side effect.
            raise Problem("仍在等待当前步骤安全暂停；未强制终止，原配置保持不变，请稍后查看状态", 409)
        await self.pumps[ident]

    def set_windows_admin(self, actor, config, value):
        if value is None:
            return
        if actor != "owner":
            raise Problem("只有用户可以修改管理员成员配置", 403)
        if type(value) is not bool:
            raise Problem("windows_admin 须为布尔值")
        if value:
            if os.name != "nt" or self.mode != "real":
                raise Problem("管理员模式只适用于 Windows 真实成员", 409)
            config["windows_admin"] = True
        else:
            config.pop("windows_admin", None)

    async def configure(self, actor, ident, *, instruction=None, model=None, grants=None, host_systemd=None, sandbox_enabled=None, mcp_services_ids=None, run_as_root=None, gpu_access=None, local_ipc=None, allow_connect=None, windows_admin=None, auto_start=None):
        self.can_manage(actor, ident)
        async with self.lock(ident):
            update = None
            # Serialize authorization with child creation. A pending update then
            # reserves that parent's path delegation until its native receipt.
            async with self.creation_lock:
                if self.closing:
                    raise Problem("空间正在关闭，不能修改成员配置", 409)
                record = self.can_manage(actor, ident)
                if record["archived"]:
                    raise Problem("请先恢复已归档成员", 409)
                if record["status"] == "stopping":
                    raise Problem("成员正在安全暂停，完成后再修改配置", 409)
                config = dict(record["config"])
                if auto_start is not None:
                    if type(auto_start) is not bool:
                        raise Problem("auto_start 须为布尔值")
                    config["auto_start"] = auto_start
                if actor != "owner" and (windows_admin is not None or privileged(config)):
                    raise Problem("只有用户可以修改管理员成员配置", 403)
                if run_as_root is not None:
                    if actor != 'owner':
                        raise Problem('只有用户可以修改 root 身份，不向下级转授', 403)
                    if type(run_as_root) is not bool:
                        raise Problem('run_as_root 须为布尔值')
                    if run_as_root:
                        if os.name == "nt":
                            raise Problem("root 身份只适用于 Linux，请使用 Windows 管理员模式", 409)
                        if self.mode != 'real':
                            raise Problem('演示空间不支持 root 身份', 409)
                        await root_worker.check(self.root)
                    config['run_as_root'] = run_as_root
                if ident not in self.workers:
                    await self.release_root_runtime(record)
                self.set_windows_admin(actor, config, windows_admin)
                if allow_connect is not None:
                    if type(allow_connect) is not bool or not config.get("relay_namespace"):
                        raise Problem("此选项仅适用于使用套件的新成员")
                    config["allow_connect"] = allow_connect
                self.set_local_ipc(config, local_ipc)
                if mcp_services_ids is not None:
                    self.refresh_mcp_catalog()
                    chosen = mcp_services.selection(self.mcp_catalog, mcp_services_ids)
                    if chosen != sorted(self.service_ids(record)):
                        config["mcp_services"] = chosen
                for key, value in (("host_systemd", host_systemd), ("sandbox_enabled", sandbox_enabled)):
                    if value is not None:
                        if type(value) is not bool:
                            raise Problem(key + " 须为布尔值")
                        config[key] = value
                self.set_gpu_permission(config, gpu_access)
                if instruction is not None:
                    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 20000:
                        raise Problem("instruction 须为 1–20000 字")
                    config["instruction"] = instruction
                if model is not None:
                    if model not in self.models:
                        raise Problem("模型不在宿主 allowlist 中", 403)
                    config["model"] = model
                if grants is not None:
                    if record["parent"] and self.permission_pending(record["parent"]):
                        raise Problem("上级路径权限正在更新，请等待生效后再修改下级授权", 409)
                    config["grants"] = self.grants(actor, grants, workspace=self.workspace(record), previous=record["config"]["grants"])
                self.validate_suite_permissions(config)
                self.check_permission_delegation(actor, record, config)
                paths_changed = config["grants"] != record["config"]["grants"]
                permissions_changed = (paths_changed or config.get("sandbox_enabled", True) != record["config"].get("sandbox_enabled", True)
                                       or config.get('gpu_access', False) != record['config'].get('gpu_access', False)
                                       or config.get('local_ipc', False) != record['config'].get('local_ipc', False)) and not privileged(config) and not privileged(record['config'])
                if self.permission_pending(ident) and (permissions_changed or ident in self.workers):
                    raise Problem("已有权限变更等待生效；请等待当前步骤结束，离线成员请先恢复运行", 409)
                if paths_changed:
                    before = workspace_policy(self.workspace(record), record["config"]["grants"])
                    after = workspace_policy(self.workspace(record), config["grants"])
                    for child in self.store.agents():
                        if child["parent"] != ident:
                            continue
                        if self.permission_pending(child["id"]):
                            raise Problem("下级路径权限正在更新，请等待生效后再收窄上级权限", 409)
                        if not retains_delegated_grants(before, after, child["config"]["grants"]):
                            raise Problem("下级仍持有该路径授权，请先收窄下级权限", 409)
                self.rules({**record, "config": config})
                self.materialize_grants(config["grants"])
                running = ident in self.workers
                mcp_changed = self.service_ids(record) != self.service_ids({**record, "config": config})
                other_changed = any(config.get(key) != record["config"].get(key)
                                    for key in set(config) | set(record["config"])
                                    if key not in {"grants", "sandbox_enabled", "host_systemd", "run_as_root", "gpu_access", "local_ipc", "auto_start"}) or config.get('run_as_root', False) != record['config'].get('run_as_root', False)
                connection_grant_changed = config.get("allow_connect", False) != record["config"].get("allow_connect", False)
                mcp_only = (mcp_changed or connection_grant_changed) and not permissions_changed and all(
                    config.get(key) == record["config"].get(key)
                    for key in set(config) | set(record["config"]) if key not in {"mcp_services", "allow_connect", "auto_start"})
                added = set(self.service_ids({**record, "config": config})) - set(self.service_ids(record))
                needs_credentials = mcp_services.environment_keys({name: self.mcp_catalog[name]["server"] for name in added})
                live_mcp = mcp_only and not needs_credentials and self.worker_capabilities.get(ident, {}).get("live_mcp")
                if running and ((permissions_changed and not other_changed) or live_mcp):
                    if not live_mcp and not self.worker_capabilities.get(ident, {}).get("live_filesystem_policy"):
                        raise Problem("此成员运行时尚未支持在线权限更新，请先重载空间升级；配置未更改", 409)
                    update = {"id": uuid.uuid4().hex, "status": "pending", "created": time.time(),
                              "previous": record["config"]}
                    if live_mcp:
                        update["kind"] = "mcp"
                    self.save_permission_update(ident, update)
                    self.store.update(ident, config=config)
                    try:
                        self.prepare(self.store.agent(ident))
                    except Exception:
                        self.store.update(ident, config=record["config"])
                        update.update(status="failed", details={"error": "无法准备权限配置，未发送到运行时"})
                        self.save_permission_update(ident, update)
                        raise
                    self.store.audit(actor, "permissions_requested", ident)
                elif running and other_changed:
                    # Do not hold the creation lock while waiting for a Step to
                    # finish: that Step may itself be creating a child via MCP.
                    self.configuration_reservations.add(ident)
                else:
                    self.store.update(ident, config=config)
                    if permissions_changed or other_changed or not running:
                        self.prepare(self.store.agent(ident))
                    self.store.audit(actor, "configure", ident)
                    return {**self.view(self.store.agent(ident)), "configuration_update": "saved"}
            if update:
                return await self.apply_live_permissions(self.store.agent(ident), update)
            try:
                await self._stop(ident)
                if self.store.agent(ident)["error"]:
                    raise Problem("agent 未正常暂停；配置未更改，请先处理恢复错误", 409)
                async with self.creation_lock:
                    self.store.update(ident, config=config)
                    self.prepare(self.store.agent(ident))
                try:
                    await self._start(ident)
                except AdminLaunchProblem as error:
                    if not privileged(record["config"]):
                        async with self.creation_lock:
                            self.store.update(ident, config=record["config"])
                            self.prepare(self.store.agent(ident))
                        await self._start(ident)
                        return {**self.view(self.store.agent(ident)),
                                "configuration_update": "rolled_back", "startup_warning": str(error)}
                    return {**self.view(self.store.agent(ident)),
                            "configuration_update": "saved", "startup_warning": str(error)}
                self.store.audit(actor, "configure", ident)
                return {**self.view(self.store.agent(ident)), "configuration_update": "restart"}
            finally:
                self.configuration_reservations.discard(ident)

    async def host_systemd(self, actor, program, arguments, timeout=30):
        record = self.store.agent(actor)
        if record["archived"] or record["config"].get("host_systemd") is not True:
            raise Problem("此成员尚未获授宿主用户级 systemd 管理权", 403)
        if self.mode != "real":
            raise Problem("演示空间不能执行宿主 systemd 命令", 403)
        from .host_systemd import execute
        # Arguments/output can contain private application information: audit
        # identity and executable, not raw arguments or output.
        self.store.audit(actor, "host_systemd:" + str(program), actor)
        return await execute(program, arguments, timeout)

    async def archive(self, actor, ident, restore=False):
        record = self.can_manage(actor, ident)
        if restore and record["parent"] and self.store.agent(record["parent"])["archived"]:
            raise Problem("请先恢复 parent", 409)
        if not restore and any(a["parent"] == ident and not a["archived"] for a in self.store.agents()):
            raise Problem("请先归档或处理其直接 child", 409)
        async with self.lock(ident):
            await self._stop(ident)
            # The owner controls this default Relay domain. No credentials are returned.
            await self.relay(self.owner_relay_token, "POST", "/api/accounts/" + record["relay_account"], {"action": "enable" if restore else "disable"})
            self.store.update(ident, archived=int(not restore), status="offline" if restore else "archived", error=None, run_requested=0)
        self.store.audit(actor, "restore" if restore else "archive", ident)
        return self.view(self.store.agent(ident))

    async def bulk(self, action):
        if action not in {"start", "stop"}:
            raise Problem("未知批量操作")
        agents = [a for a in self.store.agents() if not a["archived"]]
        if action == "stop":
            agents.reverse()
        succeeded, failed = [], []
        for record in agents:
            try:
                await getattr(self, action)("owner", record["id"])
                succeeded.append(record["handle"])
            except Exception as error:
                failed.append({"handle": record["handle"], "error": str(error)})
        return {"succeeded": succeeded, "failed": failed}

    async def reconnect_relay(self, actor, ident):
        record = self.can_manage(actor, ident)
        if actor != "owner" and privileged(record["config"]):
            raise Problem("只有用户可以修改管理员成员配置", 403)
        manager_token = self.owner_relay_token if actor == "owner" else json.loads(self.credentials(actor).read_text(encoding="utf-8"))["relay"]
        accounts = await self.relay(manager_token, "GET", "/api/accounts")
        account = next((a for a in accounts["accounts"] if a["id"] == record["relay_account"]), None)
        if not account or not account["active"]:
            raise Problem("请先在 Relay 中启用该账号及其管理者", 409)
        async with self.lock(ident):
            if actor != "owner" and privileged(self.store.agent(ident)["config"]):
                raise Problem("只有用户可以修改管理员成员配置", 403)
            running = ident in self.workers
            await self._stop(ident)
            reset = await self.relay(manager_token, "POST", "/api/accounts/" + record["relay_account"], {"action": "reset_token"})
            credentials = json.loads(self.credentials(ident).read_text(encoding="utf-8"))
            credentials["relay"] = reset["token"]
            secret_file(self.credentials(ident), json.dumps(credentials))
            self.prepare(self.store.agent(ident))
            if running and not self.store.agent(ident)["config"].get("windows_admin"):
                await self._start(ident)
        self.store.audit(actor, "reconnect_relay", ident)
        return {"ok": True, "account": record["relay_account"]}

    async def flush(self, ident):
        proc = self.workers.get(ident)
        if not proc or self.store.agent(ident)["status"] != "online":
            return
        for message in self.store.pending(ident):
            if message["id"] in self.inflight[ident]:
                continue
            payload = {"op": "terminal", "text": message["text"], "id": message["id"]} if message["kind"] == "terminal" else {"op": "management", "message": message, "id": message["id"]}
            try:
                proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
                self.inflight[ident].add(message["id"])
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self.inflight[ident].discard(message["id"])
                return

    async def send(self, actor, target, text, terminal=False):
        record = self.store.agent(target)
        if terminal:
            if actor != "owner":
                raise Problem("终端仅供用户使用", 403)
        elif actor != "owner":
            me = self.store.agent(actor)
            if target != me["parent"] and record["parent"] != actor:
                raise Problem("管理消息只发给 parent 或直接 child；日常交流请用 Relay", 403)
        ident = self.store.message(actor, target, "terminal" if terminal else "management", text)
        if terminal:
            self.store.terminal(target, "in", text)
        await self.flush(target)
        return {"id": ident, "stored": True}

    def view(self, record):
        update = self.permission_update(record["id"])
        pending = bool(update and update["status"] == "pending")
        effective = {**record, "config": update["previous"]} if pending else record
        enabled = self.sandbox_enabled(effective)
        details = (update or {}).get("details", {})
        summary = None if not update else {
            "kind": update.get("kind", "filesystem"),
            "id": update["id"], "status": update["status"], "pending": pending, "created": update["created"],
            "error": details.get("error") or update.get("error"), "policy_version": details.get("policy_version"),
            "unavailable_mcp": details.get("unavailable", []),
            "stopped_commands": len(details.get("stopped_command_handles", [])),
            "restarted_programs": len(details.get("restarted_programs", [])),
            "interrupted_tasks": bool(details.get("outcome_unknown")),
        }
        public_telemetry = {key: value for key, value in record.get("telemetry", {}).items() if key != "recent"}
        return {**record, "telemetry": public_telemetry, "workspace": str(self.workspace(record)), "relay_username": record["handle"], "instruction": self.instruction(record), "sandbox_enforced": enabled,
                "mcp_services": self.service_ids(record),
                "permission_update": summary,
                "filesystem": {"default_access": "deny" if enabled else "unrestricted",
                               "gpu_access": bool(effective['config'].get('gpu_access')) if enabled else None,
                               "local_ipc": bool(effective['config'].get('local_ipc')) if enabled else None,
                               "runtime_read_paths": list(runtime_read_paths(access_for('/', effective['config']['grants']))),
                               "effective_grants": effective["config"]["grants"],
                               "protected_paths": [str(p) for p in self.default_denied(effective)],
                               "platform_openings": self.platform_openings(effective),
                               "rule_precedence": "most_specific"}}

    def snapshot(self):
        self.refresh_mcp_catalog()
        records = self.store.agents()
        policy = self.store.restore_members()
        return {"agents": [self.view(a) for a in records], "mode": self.mode, "models": self.models, "host_systemd_available": os.name != "nt" and self.mode == "real", "sandbox_toggle_available": self.mode == "real",
                "restore_members": policy if policy is not None else any(a['config'].get('auto_start') is True for a in records),
                "local_ipc_available": sys.platform.startswith('linux') and self.mode == 'real',
                "gpu_available": self.mode == 'real' and bool(gpu_devices()),
                **({"windows_admin_available": self.mode == "real"} if os.name == "nt" else {}),
                "root_available": os.name != "nt" and self.mode == 'real' and root_worker.available(self.root),
                "mcp_services": mcp_services.public_catalog(self.mcp_catalog),
                "name": getattr(self, "name", "Squad"),
                "relay_url": self.relay_url, "relay_domain": self.domain, "user_account": self.user_account,
                "audit": self.store.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 100")}

    async def close(self):
        async with self.creation_lock:
            self.closing = True
        async def checkpoint(ident):
            # Include starts already in flight, not just workers visible now.
            async with self.lock(ident):
                await self._stop(ident, timeout=None)
        await asyncio.gather(*(checkpoint(a["id"]) for a in self.store.agents()))
        self.store.db.close()
