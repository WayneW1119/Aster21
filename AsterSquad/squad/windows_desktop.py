"""Opt-in Windows desktop supervisor. Linux entry points are unchanged."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
import webbrowser


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    from ._windows import private_file
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    private_file(temporary)
    try:
        # Windows readers may briefly hold the target without delete sharing.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 9:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def credentials(root, value=None):
    import win32crypt
    from ._windows import private_file
    path = root / "credentials.dpapi"
    if value is not None:
        encrypted = win32crypt.CryptProtectData(json.dumps(value).encode(), "Aster Windows", None, None, None, 1)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encrypted)
        private_file(temporary)
        os.replace(temporary, path)
    return json.loads(win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 1)[1])


def initialize(root):
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "desktop.json"
    if config_path.exists():
        raise RuntimeError("Already initialized; existing configuration was preserved")
    key_env = os.environ.get("ASTER_STUDIO_API_KEY_ENV", "DEEPSEEK_API_KEY")
    if not key_env.isascii() or not key_env.isidentifier() or key_env in {'ASTER_WEB_TOKEN', 'ASTER_IMAGES_TOKEN'}:
        raise ValueError('Choose a valid, separate model API key environment name')
    key = os.environ.get(key_env, "")
    model = os.environ.get("ASTER_STUDIO_MODEL", "deepseek-flash")
    url = os.environ.get("ASTER_STUDIO_MODEL_URL", "https://api.deepseek.com/responses")
    codex = shutil.which("codex.exe")
    config = {"version": 1, "name": "Windows Studio", "model": model, "url": url,
              "api_key_env": key_env, "model_id": os.environ.get("ASTER_STUDIO_MODEL_ID", "deepseek" if key_env == "DEEPSEEK_API_KEY" else "model"),
              "web_enabled": False,
              "ports": {"console": 21910, "squad": 21912, "squad_mcp": 21911,
                        "relay": 21920, "relay_mcp": 21921, "web": 21930, "images": 21931},
              "images_enabled": False, "codex": codex,
              "codex_home": str(Path.home() / ".codex")}
    credentials(root, {**({key_env: key} if key else {}), "ASTER_WEB_TOKEN": secrets.token_urlsafe(32),
                       "ASTER_IMAGES_TOKEN": secrets.token_urlsafe(32)})
    write(config_path, config)
    print("Initialized. Credentials are encrypted with Windows DPAPI for this account.")


def process_for(state):
    import psutil
    try:
        process = psutil.Process(state["pid"])
        return process if abs(process.create_time() - state["created"]) < .01 else None
    except (psutil.Error, KeyError):
        return None


def status(root):
    path = root / "desktop-state.json"
    if not path.exists():
        return {"status": "stopped", "root": str(root)}
    state = read(path)
    running = process_for(state) is not None
    return {"status": state["status"] if running else "stopped", "root": str(root),
            "url": state.get("url"), "pid": state["pid"] if running else None,
            "services": state.get("services", {}), "error": state.get("error"),
            **({"sandbox_backend": state["sandbox_backend"]} if running and state.get("sandbox_backend") else {})}


def service_enabled(config, name):
    if name not in ('web', 'images'):
        return True
    value = config.get(name + '_enabled', name == 'web')  # Preserve old web presets.
    if type(value) is not bool:
        raise ValueError(name + '_enabled must be boolean')
    return value


def configure_catalogs(root, config):
    key_env = config.get('api_key_env', 'DEEPSEEK_API_KEY')
    models = {}
    if os.environ.get(key_env) or credentials(root).get(key_env):
        models[config.get('model_id', 'deepseek')] = {
            'name': config['model'], 'url': config['url'], 'api_key_env': key_env}
    if account := config.get("chatgpt"):
        from aster.auth import ChatGPTAccount
        home = Path(account["home"]).expanduser().resolve(strict=True)
        profile = account.get("profile", "default")
        ChatGPTAccount(home, profile)  # Validate the profile name without reading tokens.
        for model in ("gpt-6.1-sol", "gpt-6.2-astra", "gpt-6.2-sol"):
            models[model] = {
                "name": model, "url": "https://api.openai.com/v1/responses",
                "auth": "chatgpt", "auth_home": str(home), "auth_profile": profile,
            }
    write(root / "models.json", models)
    ports = config["ports"]
    catalog = {}
    if service_enabled(config, 'web'):
        catalog['web'] = {"name": "Web search and fetch", "default": False, "server": {
            "url": f"http://127.0.0.1:{ports['web']}/mcp", "bearer_token_env_var": "ASTER_WEB_TOKEN"}}
    if config["images_enabled"]:
        catalog["images"] = {"name": "Codex images", "default": False, "server": {
            "command": sys.executable, "args": ["-P", "-m", "aster_images", "mcp"],
            "env_vars": ["ASTER_IMAGES_URL", "ASTER_IMAGES_TOKEN"], "required": False,
            "tool_timeout_sec": 480}}
    # Preserve host-added entries in the same catalog watched by Squad.
    from .mcp_services import clean, load_catalog
    catalog_path = root / "mcp-services.json"
    if catalog_path.exists():
        catalog.update({name: entry for name, entry in load_catalog(catalog_path).items()
                        if name not in {"web", "images"}})
    write(catalog_path, clean(catalog))


def preflight(root, config):
    packages = ["squad", "relay", "aster", "win32api", "psutil"]
    if service_enabled(config, 'web'):
        packages.append('aster_web')
    if config["images_enabled"]:
        packages.append("aster_images")
    for package in packages:
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(f"Missing dependency: {package}")
    key_env = config.get('api_key_env', 'DEEPSEEK_API_KEY')
    if not (os.environ.get(key_env) or credentials(root).get(key_env)) and not config.get('chatgpt'):
        raise RuntimeError('Configure a model API key or ChatGPT account before starting')
    ports = [port for name,port in config['ports'].items() if service_enabled(config, name)]
    if len(ports) != len(set(ports)) or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
        raise RuntimeError("Configure distinct ports in desktop.json (1024..65535)")
    for name, port in config["ports"].items():
        if not service_enabled(config, name):
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Port occupied: {name} ({port}); no existing service was stopped") from None
    if config["images_enabled"]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI_")
               and k not in {"CODEX_API_KEY", "CODEX_ACCESS_TOKEN"}}
        env["CODEX_HOME"] = config["codex_home"]
        check = subprocess.run([config["codex"], "login", "status"], env=env, capture_output=True,
                               text=True, encoding="utf-8", timeout=25)
        if check.returncode or "ChatGPT" not in check.stdout + check.stderr:
            raise RuntimeError("Images needs a native Codex ChatGPT login; or set images_enabled=false")


async def serve_service(root, name):
    from ._windows import install_stop_handler
    config = read(root / "desktop.json")
    if name == "web":
        import uvicorn
        from aster_web.server import build, Auth
        from aster_web.service import Service
        app = Auth(build(Service(deepseek_key=os.environ.get("DEEPSEEK_API_KEY", ""),
                                 exa_key=os.environ.get("EXA_API_KEY", ""))).streamable_http_app(),
                   os.environ["ASTER_WEB_TOKEN"])
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config["ports"][name],
                                              log_level="warning", access_log=False))
        install_stop_handler(asyncio.get_running_loop(), lambda: setattr(server, "should_exit", True))
        await server.serve()
    else:
        from aiohttp import web
        from aster_images.server import backend
        from aster_images.runner import Runner
        worker = Runner(root / "image-jobs", codex=config["codex"], codex_home=config["codex_home"])
        runner = web.AppRunner(backend(worker, os.environ["ASTER_IMAGES_TOKEN"]))
        stop = asyncio.Event()
        install_stop_handler(asyncio.get_running_loop(), stop.set)
        try:
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", config["ports"][name]).start()
            await stop.wait()
        finally:
            await runner.cleanup()


async def supervise(root):
    import psutil
    import aiohttp
    from ._windows import spawn, request_stop, install_stop_handler
    config = read(root / "desktop.json")
    env = dict(os.environ, **credentials(root), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    key_env = config.get("api_key_env", "DEEPSEEK_API_KEY")
    if key := os.environ.get(key_env):
        env[key_env] = key
    env["ASTER_IMAGES_URL"] = f"http://127.0.0.1:{config['ports']['images']}"
    ports = config["ports"]
    state = {"pid": os.getpid(), "created": psutil.Process().create_time(), "status": "starting",
             "instance": os.environ["ASTER_DESKTOP_INSTANCE"],
             "stop_event": os.environ["ASTER_HOST_STOP_EVENT"],
             "url": f"http://127.0.0.1:{ports['console']}/squad/", "services": {}}
    if env.get("ASTER_MXC_EXECUTOR"):
        state["sandbox_backend"] = "mxc"
    stop = asyncio.Event()
    install_stop_handler(asyncio.get_running_loop(), stop.set)
    processes = {}
    def save():
        write(root / "desktop-state.json", state)
    save()
    try:
        commands = {}
        if service_enabled(config, 'web'):
            commands['web'] = [sys.executable, "-P", "-m", "squad.windows_desktop", "service",
                               "--root", str(root), "--service", "web"]
        if config["images_enabled"]:
            commands["images"] = [sys.executable, "-P", "-m", "squad.windows_desktop", "service",
                                  "--root", str(root), "--service", "images"]
        commands["console"] = [sys.executable, "-P", "-m", "squad.fleet", "--root", str(root / "studio"),
            "--desktop-root", str(root),
            "--mode", "real", "--name", config["name"], "--port", str(ports["console"]),
            "--primary-port", str(ports["squad"]), "--mcp-port", str(ports["squad_mcp"]),
            "--relay-port", str(ports["relay"]), "--relay-mcp-port", str(ports["relay_mcp"]),
            "--model-catalog", str(root / "models.json"), "--mcp-catalog", str(root / "mcp-services.json")]
        if env.get("ASTER_MXC_EXECUTOR"):
            private_roots = [root, *map(Path, config.get("mxc_private_roots", []))]
            if config.get("codex_home") and Path(config["codex_home"]).is_dir():
                private_roots.append(Path(config["codex_home"]))
            for private_root in dict.fromkeys(path.resolve(strict=True) for path in private_roots):
                commands["console"].extend(["--private-root", str(private_root)])
        for name, command in commands.items():
            with (root / "logs" / (name + ".log")).open("ab") as log:
                processes[name] = await spawn(*command, env=env, stdout=log, stderr=log, cwd=root)
            state["services"][name] = {"pid": processes[name].pid, "port": ports[name], "status": "starting"}
            save()
        deadline = time.monotonic() + 75
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as http:
            while not stop.is_set():
                for name, process in processes.items():
                    if process.returncode is not None:
                        raise RuntimeError(f"{name} exited ({process.returncode}); see logs/{name}.log")
                ready = True
                for name in processes:
                    try:
                        if name == "console":
                            token_file = root / "studio/control/owner.token"
                            if not token_file.exists():
                                ready = False
                                continue
                            token = token_file.read_text(encoding="utf-8").strip()
                            async with http.get(f"http://127.0.0.1:{ports[name]}/squad/api/state",
                                                headers={"Authorization": "Bearer " + token}) as response:
                                value = await response.json()
                                ok = response.status == 200 and any(r["status"] == "running" for r in value["resources"])
                        else:
                            # Unauthorized HTTP proves the service is listening without consuming API quota.
                            path = "/mcp" if name == "web" else "/generate"
                            async with http.get(f"http://127.0.0.1:{ports[name]}{path}") as response:
                                ok = response.status == 401
                        if ok:
                            state["services"][name]["status"] = "ready"
                        else:
                            ready = False
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError):
                        ready = False
                if ready:
                    state["status"] = "running"
                    save()
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Readiness timeout; see desktop logs and studio/control/fleet/logs")
                await asyncio.sleep(.3)
        while not stop.is_set():
            for name, process in processes.items():
                if process.returncode is not None:
                    raise RuntimeError(f"{name} exited ({process.returncode}); stopping remaining owned services")
            try:
                await asyncio.wait_for(stop.wait(), 1)
            except asyncio.TimeoutError:
                pass
    except Exception as error:
        state["error"] = str(error)
    finally:
        state["status"] = "stopping"
        save()
        # Console waits for members before Web/Images are stopped.
        for name in ["console", "images", "web"]:
            if name not in processes:
                continue
            process = processes[name]
            if process.returncode is None:
                request_stop(process)
                await process.wait()  # Deliberately no forced timeout/kill of active member work.
            state["services"][name].update(status="stopped", exit_code=process.returncode)
            save()
        state["status"] = "stopped"
        save()


def configured_mxc(config, override=None):
    executor = override if override is not None else config.get("mxc_executor")
    if executor is None:
        return None
    from aster.permissions import probe_windows_sandbox as diagnose
    if diagnose(executor)["status"] != "compatible_for_smoke_test":
        raise RuntimeError("This Windows host did not pass the native MXC compatibility probe")
    return Path(executor).resolve(strict=True)


def start(root, *, mxc_executor=None):
    current = status(root)
    if current["status"] != "stopped":
        raise RuntimeError("Desktop already running or stopping; use status")
    config = read(root / "desktop.json")
    mxc_executor = configured_mxc(config, mxc_executor)
    preflight(root, config)
    configure_catalogs(root, config)
    (root / "logs").mkdir(exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               ASTER_HOST_STOP_EVENT="Local\\AsterDesktop-" + uuid.uuid4().hex,
               ASTER_DESKTOP_INSTANCE=uuid.uuid4().hex)
    if mxc_executor is not None:
        env["ASTER_MXC_EXECUTOR"] = str(mxc_executor)
    with (root / "logs/desktop.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-P", "-m", "squad.windows_desktop", "supervise", "--root", str(root)],
            env=env, cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    for _ in range(180):
        current = status(root)
        state_path = root / "desktop-state.json"
        if (state_path.exists() and read(state_path).get("instance") == env["ASTER_DESKTOP_INSTANCE"]
                and current["status"] == "running"):
            return current
        if process.poll() is not None:
            raise RuntimeError(current.get("error") or "Supervisor exited; see logs/desktop.log")
        time.sleep(.5)
    raise RuntimeError("Startup still pending; use status or stop (no services force-killed)")


def stop(root):
    import win32event
    import win32con
    current = status(root)
    if current["status"] == "stopped":
        return current
    state = read(root / "desktop-state.json")
    process = process_for(state)
    if process is None:
        return status(root)
    event = win32event.OpenEvent(win32con.EVENT_MODIFY_STATE, False, state["stop_event"])
    try:
        win32event.SetEvent(event)
    finally:
        event.Close()
    try:
        process.wait(timeout=180)
    except __import__("psutil").TimeoutExpired:
        raise RuntimeError("Still waiting for safe shutdown; use status; no process was force-killed") from None
    return status(root)


def main():
    if os.name != "nt":
        raise SystemExit("This optional desktop launcher is Windows-only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "start", "stop", "restart", "status", "open", "window", "supervise", "service",
                                           "sandbox-mxc-probe", "sandbox-mxc-smoke"])
    parser.add_argument("--root", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "AsterWindows")
    parser.add_argument("--service", choices=["web", "images"])
    parser.add_argument("--page", choices=["squad", "relay"], default="squad")
    parser.add_argument("--selected", default="", help=argparse.SUPPRESS)
    parser.add_argument("--mxc-executor", type=Path)
    args = parser.parse_args()
    if args.action in ("sandbox-mxc-probe", "sandbox-mxc-smoke"):
        if args.mxc_executor is None:
            parser.error(f"{args.action} requires --mxc-executor")
        if args.action == "sandbox-mxc-probe":
            from aster.permissions import probe_windows_sandbox as diagnose
            report = diagnose(args.mxc_executor)
        else:
            from aster.permissions import smoke_windows_sandbox as smoke
            report = smoke(args.mxc_executor)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.action == "sandbox-mxc-smoke" and report["status"] != "smoke_passed":
            raise SystemExit(1)
        return
    if args.mxc_executor is not None and args.action not in ("start", "restart"):
        parser.error("--mxc-executor requires start, restart, or an MXC diagnostic")
    root = args.root.expanduser().resolve()
    if args.action == "supervise":
        asyncio.run(supervise(root))
        return
    if args.action == "service":
        asyncio.run(serve_service(root, args.service))
        return
    if args.action == "init":
        initialize(root)
        return
    if args.action == "window":
        from .windows_window import run_window
        run_window(root, args.page, args.selected)
        return
    # Serialize mutations from independent PowerShell invocations.
    from . import _windows_locks as locks
    with (root / "desktop.lock").open("a+b") as lock:
        locks.flock(lock, locks.LOCK_EX | locks.LOCK_NB)
        if args.action == "status":
            result = status(root)
        elif args.action in {"stop", "restart"}:
            if args.action == "restart":
                configured_mxc(read(root / "desktop.json"), args.mxc_executor)
            result = stop(root)
            if args.action == "restart":
                result = start(root, mxc_executor=args.mxc_executor)
        elif args.action == "start":
            result = start(root, mxc_executor=args.mxc_executor)
        else:
            result = status(root)
            if result["status"] != "running":
                raise RuntimeError("Start desktop before opening it")
            token = (root / "studio/control/owner.token").read_text(encoding="utf-8").strip()
            webbrowser.open(result["url"] + "#token=" + token)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        if sys.stderr is None and os.name == "nt":
            import win32api
            win32api.MessageBox(0, str(error), "Aster Windows Studio", 0x10)
        else:
            print("Aster desktop: " + str(error), file=sys.stderr)
        raise SystemExit(1)
