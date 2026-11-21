"""Host-owned three-mode filesystem policy and a common execution boundary."""

from copy import deepcopy
from functools import lru_cache
from contextlib import contextmanager
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import RLock

from .policy import access_for, TIE

MODES = {"read", "write", "deny"}
RESOLV_CONF = Path("/etc/resolv.conf")
HOST_BRIDGES = (Path("/run/WSL"), Path("/mnt/wslg"), Path("/mnt/wsl"), Path("/init"))
LINUX_RUNTIME = ("/usr/bin", "/usr/sbin", "/usr/lib", "/usr/lib64", "/usr/local/bin",
                 "/usr/local/lib", "/usr/share/zoneinfo", "/usr/share/terminfo",
                 "/etc/ld.so.cache", "/etc/nsswitch.conf", "/etc/hosts",
                 "/etc/ssl/certs", "/etc/ssl/openssl.cnf")


def gpu_devices():
    """Supported GPU devices on this host; never infer access from file grants."""
    path = Path('/dev/dxg')
    return (str(path),) if path.is_char_device() and path.resolve() == path else ()


@lru_cache(maxsize=3)
def runtime_read_paths(root_access="deny"):
    """Host-selected runtime roots; a read-only ceiling, never user data grants."""
    if sys.platform == 'win32':
        roots = [Path(__file__).resolve().parents[2], Path(sys.prefix), Path(sys.base_prefix),
                 Path(os.environ['SystemRoot'])]
        return tuple(dict.fromkeys(str(p.resolve()) for p in roots if p.exists()))
    package = Path(__file__).resolve().parents[2]
    candidates = [package, package.parent / "aster", *map(Path, LINUX_RUNTIME)]
    prefix, base = Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()
    if prefix != Path("/") and (prefix != base or root_access != "deny"):
        candidates.append(prefix)
    if base != Path("/"):
        candidates += [base / "bin", base / "lib", Path(sys._base_executable)]
    return tuple(dict.fromkeys(str(p.resolve()) for p in candidates if p.exists() and p.resolve() != Path("/")))


def parse_policy(value, *, legacy=False):
    if value is None:
        return None  # Legacy/explicitly disabled host configuration.
    if legacy and isinstance(value, dict) and 'format' not in value and 'default_access' in value:
        value = dict(value)
        default = value.pop('default_access')
        if default not in ('read', 'deny'):
            raise ValueError('Invalid legacy filesystem default')
        if default == 'deny':
            value['format'] = 2  # Do not synthesize the legacy global-read rule.
    if (
        not isinstance(value, dict)
        or set(value) - {"rules", "version", "format", "gpu", "local_ipc"}
        or not isinstance(value.get("rules", []), (list, tuple))
    ):
        raise ValueError("filesystem_policy requires a rules list")
    if value.get("format", 2) != 2 or type(value.get("format", 2)) is not int:
        raise ValueError("Unsupported filesystem policy format")
    if type(value.get('gpu', False)) is not bool:
        raise ValueError('GPU device permission must be boolean')
    if type(value.get('local_ipc', False)) is not bool:
        raise ValueError('Local IPC permission must be boolean')
    rules = {"/": {"path": "/", "access": "read"}} if legacy and "format" not in value else {}
    supplied = value.get('rules', [])
    if len(supplied) > 65 or (len(supplied) == 65 and not any(isinstance(r, dict) and r.get('path') == '/' for r in supplied)):
        raise ValueError("At most 64 filesystem rules plus the root rule")
    for entry in value.get("rules", []):
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "access"}
            or not isinstance(entry["path"], str)
            or not entry["path"]
            or "\0" in entry["path"]
            or not isinstance(entry["access"], str)
            or entry["access"] not in MODES
        ):
            raise ValueError("Filesystem rules require path and read/write/deny access")
        previous = rules.get(entry["path"])
        if previous is None or TIE[entry["access"]] > TIE[previous["access"]]:
            rules[entry["path"]] = dict(entry)
    version = value.get("version", 1)
    if type(version) is not int or version < 1:
        raise ValueError("Invalid filesystem policy version")
    return {"rules": list(rules.values()), "version": version, "format": 2,
            **({key: True for key in ('gpu', 'local_ipc') if value.get(key)})}


class FilesystemSandbox:
    def __init__(self, workspace, policy, *, private_roots=()):
        self.workspace = Path(workspace).resolve(strict=True)
        self.private_roots = tuple(Path(p).resolve() for p in private_roots)
        self.package = Path(__file__).resolve().parents[2]
        self.launcher = Path(__file__).with_name("launcher.py")
        self.worker = Path(__file__).with_name("worker.py")
        self.policy = parse_policy(policy)
        self.version = self.policy["version"] if self.policy is not None else 1
        self.lock = RLock()
        self._windows_runtime = None
        if self.policy is not None:
            self.materialize(self.policy)

    @property
    def enabled(self):
        return self.policy is not None

    @property
    def windows_mxc_enabled(self):
        return sys.platform == "win32" and self.enabled and self._windows_runtime is not None

    @property
    def spawn_windows_exec(self):
        return self._windows_runtime.spawn_windows_exec if self.windows_mxc_enabled else None

    @property
    def windows_mxc_shell(self):
        return str(self._windows_runtime.powershell) if self.windows_mxc_enabled else None

    def _mxc(self, policy):
        if self._windows_runtime is None:
            executor = os.environ.get("ASTER_MXC_EXECUTOR")
            if not executor:
                raise RuntimeError("Windows preview has no filesystem sandbox configured; set ASTER_MXC_EXECUTOR to opt in")
            from agent_harness.experimental.windows_mxc_runtime import WindowsMxcRuntime
            system = Path(os.environ["SystemRoot"])
            shell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
            self._windows_runtime = WindowsMxcRuntime(
                executor, self.workspace, policy,
                python=sys.executable, powershell=os.environ.get("ASTER_MXC_POWERSHELL", shell),
                private_roots=(*self.private_roots, *[p for p in (self.package.parent / ".env",) if p.exists()]),
            )
        return self._windows_runtime

    def materialize(self, policy, cwd=None, grants=()):
        policy = parse_policy(policy)
        if policy is None:
            raise ValueError("An active sandbox cannot be disabled during a Session")
        if sys.platform == 'win32':
            from agent_harness.experimental.windows_mxc_policy import compile_filesystem
            runtime = self._mxc(policy)
            return compile_filesystem(
                self.workspace, policy, cwd=cwd,
                runtime_roots=runtime.runtime_roots, private_roots=runtime.private_roots,
                role_grants=grants,
            )
        if not sys.platform.startswith("linux") or not Path("/usr/bin/bwrap").is_file():
            raise RuntimeError(
                "Filesystem sandbox requires Linux/WSL2 and /usr/bin/bwrap"
            )
        entries = {}
        for entry in policy["rules"]:
            path = (self.workspace / entry["path"]).absolute()
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise ValueError("Filesystem rules cannot cross symlinks: " + str(path))
            path = path.resolve(strict=True)
            name = str(path)
            previous = entries.get(name)
            if previous is None or TIE[entry["access"]] > TIE[previous]:
                entries[name] = entry["access"]
        # Workspace access is a default, overridden by an exact explicit rule.
        entries.setdefault(str(self.workspace), "write")
        rules = [{"path": path, "access": access} for path, access in entries.items()]
        exception_rules = [r for r in rules if r['path'] != '/']
        private_tmp = []
        if policy.get('local_ipc'):
            for name in ('/tmp', '/var/tmp'):
                path = Path(name)
                if any(path.is_relative_to(Path(r['path'])) for r in exception_rules):
                    continue
                if not path.is_dir() or path.resolve() != path:
                    raise ValueError('Local IPC requires canonical temporary directories: ' + name)
                entries[name] = 'write'
                private_tmp.append(name)
        devices = gpu_devices() if policy.get('gpu') else ()
        if policy.get('gpu') and not devices:
            raise RuntimeError('GPU access currently requires a WSL2 /dev/dxg device')
        for device in devices:
            if access_for(device, exception_rules, default='read') == 'deny' or any(
                    Path(device).is_relative_to(p) or p.is_relative_to(Path(device)) for p in self.private_roots):
                raise ValueError('GPU device conflicts with a denied or private path')
        # Synthetic source/state grants are host-owned exceptions. Only an
        # explicit rule can forbid their source; unmatched paths stay private.
        for path, access in entries.items():
            if path == "/" or access == "deny":
                continue
            if any(Path(path).is_relative_to(Path(reserved)) or Path(reserved).is_relative_to(Path(path))
                   for reserved in ("/proc", "/dev", "/run")):
                raise ValueError("Policy mounts cannot replace reserved runtime views")
        for grant in grants:
            if access_for(grant["source"], exception_rules, default="read") == "deny":
                raise ValueError("Role grant conflicts with an explicit deny rule")
        denied = []
        for root in self.private_roots:
            if root.exists():
                denied.append(str(root))
        # Keep the controller, model credentials and WSL host bridges outside tool authority.
        credential = self.package.parent / ".env"
        for path in (credential, *HOST_BRIDGES):
            if path.exists():
                denied.append(str(path.resolve()))
        protected = list(runtime_read_paths(access_for("/", rules)))
        denied = sorted(set(denied), key=lambda p: (len(Path(p).parts), p))
        denied = [p for p in denied if not any(Path(p).is_relative_to(Path(parent)) for parent in denied if parent != p)]
        for path, access in entries.items():
            if access == "write" and any(Path(path).is_relative_to(Path(p)) for p in denied):
                raise ValueError("Writable roots cannot reopen a protected subtree")
        # Host-provided runtime roots are specific read exceptions. Exact user
        # entries and narrower denies remain effective; private roots win last.
        for path in protected:
            entries.setdefault(path, 'read')
        for path, access in list(entries.items()):
            if access == 'write' and any(Path(path).is_relative_to(Path(p)) for p in protected):
                entries[path] = 'read'
        # Remove only rules identical to their nearest ancestor. A deny below
        # a reopened subtree remains necessary and must not be collapsed away.
        plan = []
        for path, access in sorted(entries.items(), key=lambda e: (len(Path(e[0]).parts), e[0])):
            ancestors = [r for r in plan if r['path'] != path]
            if any(Path(path).is_relative_to(Path(p)) for p in private_tmp) or access_for(path, ancestors) != access:
                plan.append({'path': path, 'access': access})
        if not any(e['path'] == '/' for e in plan):
            plan.insert(0, {'path': '/', 'access': 'deny'})
        cwd = str(Path(cwd or self.workspace).resolve(strict=True))
        if access_for(cwd, rules) == 'deny' or any(Path(cwd).is_relative_to(Path(p)) for p in denied):
            raise ValueError("Command working directory is denied")
        # WSL and systemd-resolved commonly place resolv.conf behind a symlink
        # into a hidden bridge or /run. Expose just that infrastructure file,
        # never a private root or a path explicitly denied by the host policy.
        resolver = None
        try:
            source = RESOLV_CONF.resolve(strict=True)
            hidden = (Path("/run"), *(p.resolve() for p in HOST_BRIDGES if p.is_dir()))
            private = (*self.private_roots, credential)
            if (source.is_file()
                    and (source == RESOLV_CONF or any(source.is_relative_to(p) for p in hidden))
                    and not any(source.is_relative_to(p) or RESOLV_CONF.is_relative_to(p) for p in private)
                    and access_for(str(RESOLV_CONF), exception_rules, default="read") != "deny"
                    and access_for(str(source), exception_rules, default="read") != "deny"):
                resolver = str(source)
        except (OSError, RuntimeError):
            pass  # A missing, inaccessible or cyclic host link gains no exception.
        return {
            "cwd": cwd,
            "mounts": [r for r in plan if r['access'] != 'deny'],
            "denied": [r['path'] for r in plan if r['access'] == 'deny'],
            "protected_read": protected,
            "protected_deny": denied,
            "grants": list(grants),
            "local_ipc": policy.get("local_ipc", False),
            "private_tmp": private_tmp,
            "resolver": resolver,
            **({'devices': list(devices)} if devices else {}),
            **({"resolver_target": str(RESOLV_CONF)} if resolver and access_for(str(RESOLV_CONF), rules) == 'deny' else {}),
        }

    def environment(self, env=None):
        source = os.environ if env is None else env
        # Small explicit runtime environment; never copy model/provider credentials.
        allowed = (
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "TERM",
            "COLORTERM",
            "HARNESS_PYTHON",
            "PYTHONPATH",
        )
        result = {k: source[k] for k in allowed if k in source}
        result.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        # Avoid startup hooks and all inherited generator channels.
        return result

    def command(self, argv, *, cwd=None, env=None, grants=(), explicit_env_keys=()):
        with self.lock:
            if not self.enabled:
                return list(argv), dict(os.environ if env is None else env)
            if sys.platform == "win32":
                raise RuntimeError("Use execution() to own the lifetime of an MXC request")
            config = self.materialize(self.policy, cwd, grants)
            clean = self.environment(env)
            for key in explicit_env_keys:
                if env is not None and key in env:
                    clean[key] = env[key]
            return [
                sys.executable,
                "-I",
                "-S",
                str(self.launcher),
                json.dumps(config),
                "--",
                *map(str, argv),
            ], clean

    @contextmanager
    def execution(self, argv, *, cwd=None, env=None, grants=(), explicit_env_keys=()):
        if not self.windows_mxc_enabled:
            yield self.command(argv, cwd=cwd, env=env, grants=grants,
                               explicit_env_keys=explicit_env_keys)
            return
        with self.lock:
            runtime = self._windows_runtime
            policy = self.policy
        extra = {name: env[name] for name in explicit_env_keys if env is not None and name in env}
        with runtime.execution(argv, cwd=cwd or self.workspace, policy=policy,
                               role_grants=grants, extra_env=extra) as prepared:
            yield prepared

    def describe(self):
        if not self.enabled:
            if self.version == 1:
                return None
            return {"policy_version": self.version, "workspace": str(self.workspace),
                    "default_access": "unrestricted", "rules": [], "protected_read": [],
                    "network": "unchanged", "approvals": "never"}
        config = self.materialize(self.policy)
        if self.windows_mxc_enabled:
            runtime = self._windows_runtime
            protected = {str(path) for path in runtime.private_roots}
            return {
                "policy_version": self.version, "rule_precedence": "most_specific",
                "workspace": str(self.workspace),
                "default_access": "deny",
                "rules": [{"path": path, "access": access}
                          for field, access in (("readonlyPaths", "read"), ("readwritePaths", "write"), ("deniedPaths", "deny"))
                          for path in config[field] if path not in protected],
                "protected_read": list(dict.fromkeys(map(str, runtime.runtime_roots))),
                "protected_deny": sorted(protected),
                "network": "unchanged", "approvals": "never",
            }
        return {
            "policy_version": self.version,
            "rule_precedence": "most_specific",
            "workspace": str(self.workspace),
            "default_access": "deny",
            "rules": config["mounts"]
            + [{"path": p, "access": "deny"} for p in config["denied"]],
            "protected_read": config["protected_read"],
            "protected_deny": config["protected_deny"],
            "network": "unchanged",
            "approvals": "never",
            **({'gpu_devices': config['devices']} if config.get('devices') else {}),
            **({'local_ipc': True, 'private_tmp': config['private_tmp']} if config.get('local_ipc') else {}),
        }

    def probe(self, policy=None):
        if sys.platform == "win32":
            selected = self.policy if policy is None else parse_policy(policy)
            runtime = self._mxc(selected)
            shell = runtime.system / "System32/cmd.exe"
            with runtime.execution([str(shell), "/d", "/c", "exit", "0"], cwd=self.workspace,
                                   policy=selected, timeout=8000) as (argv, env):
                result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=15)
            if result.returncode:
                raise RuntimeError("Native MXC could not run the sandbox policy probe")
            return
        config = self.materialize(self.policy if policy is None else policy)
        command = [
            sys.executable,
            "-I",
            "-S",
            str(self.launcher),
            json.dumps(config),
            "--",
            "/bin/true",
        ]
        result = subprocess.run(
            command, env=self.environment(), capture_output=True, text=True, timeout=15
        )
        if result.returncode:
            raise RuntimeError(result.stderr[:2000] or "Sandbox probe failed")

    def activate(self, policy, *, version=None):
        with self.lock:
            self.policy = parse_policy(policy)
            if self._windows_runtime is not None:
                self._windows_runtime.policy = self.policy
            self.version = (self.policy["version"] if self.policy is not None
                            else (version if version is not None else self.version + 1))

    def file_operation(self, operation, **arguments):
        with self.lock:
            if self.windows_mxc_enabled:
                return self._windows_runtime.file_operation(operation, **arguments)
            command, env = self.command(
                [sys.executable, "-I", str(self.worker)], cwd=self.workspace
            )
            result = subprocess.run(
                command,
                input=json.dumps(
                    {
                        "operation": operation,
                        "workspace": str(self.workspace),
                        **arguments,
                    }
                ),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode:
            raise ValueError("Sandbox file operation failed: " + result.stderr[:2000])
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("Sandbox worker returned invalid output") from None
        if not value["ok"]:
            raise ValueError(value["error"])
        return value["value"]
