"""A manager-owned Python child process with bounded local IPC and logs."""

import asyncio
import os
if os.name == "nt":
    from agent_harness._windows import spawn as windows_spawn
import json
import os
from pathlib import Path
import re
import signal
import socket
import secrets
import tempfile
import time
from collections import deque
from contextlib import ExitStack

from .types import Emit
from ..runtime_environment import SDK_ROOT, python_environment


class ProgramRateLimited(Exception):
    pass


def _reject_nonfinite(value: str):
    raise ValueError(f"Non-finite JSON number: {value}")


def validate_emission(attempts: deque[float], kind: str, data: dict) -> None:
    """One validation/rate policy for real sources and isolated program tests."""
    now = time.monotonic()
    while attempts and now - attempts[0] >= 60:
        attempts.popleft()
    if sum(now - at < 10 for at in attempts) >= 5 or len(attempts) >= 20:
        raise ProgramRateLimited("Exceeded 5 emission attempts/10s or 20/60s")
    attempts.append(now)
    if (not isinstance(kind, str) or not kind.strip() or len(kind) > 128
            or not isinstance(data, dict)):
        raise ValueError("Event kind must be 1-128 characters and data a JSON object")
    if len(json.dumps({"kind": kind, "data": data}, ensure_ascii=False,
                      allow_nan=False).encode()) + 1 > 16384:
        raise ValueError("Program event exceeds 16384 bytes")


class ProgramGenerator:
    kind = "program"
    MAX_EVENT_BYTES = 16384
    MAX_LOG_BYTES = 65536

    def __init__(self, script_path: str, cwd: str, args: list[str] | None = None,
                 mode: str = "service"):
        if (not isinstance(script_path, str) or not Path(script_path).is_absolute()
                or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                or mode not in {"once", "service"}
                or (args is not None and (not isinstance(args, list) or len(args) > 64
                    or any(not isinstance(a, str) or len(a) > 4000 for a in args)))):
            raise ValueError("Use absolute script_path/cwd, string args, and mode once or service")
        self.script_path, self.cwd = script_path, cwd
        self.args, self.mode = list(args or []), mode
        self.state_dir: Path | None = None
        self.log_path: Path | None = None
        self.attempts: deque[float] = deque()
        self.process: asyncio.subprocess.Process | None = None
        self.sandbox = None

    def bind(self, ident: str, root: Path) -> None:
        if not re.fullmatch(r"gen_[0-9a-f]{32}", ident):
            raise ValueError("Invalid program generator_id")
        self.state_dir = root / ident / "state"
        self.log_path = root / ident / "output.log"

    def describe(self) -> dict:
        return {"script_path": self.script_path, "cwd": self.cwd,
                "args": self.args, "mode": self.mode,
                "state_dir": str(self.state_dir), "log_path": str(self.log_path)}

    @classmethod
    def restore(cls, configuration: dict) -> "ProgramGenerator":
        return cls(configuration["script_path"], configuration["cwd"],
                   configuration["args"], configuration["mode"])

    async def run(self, emit: Emit) -> None:
        if self.state_dir is None or self.log_path is None:
            raise ValueError("Program generator must be bound by its manager")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fatal = asyncio.get_running_loop().create_future()
        handlers: set[asyncio.Task] = set()
        clients: set[asyncio.StreamWriter] = set()
        server = None
        log_task = wait_task = None

        ipc_token = secrets.token_urlsafe(32) if os.name == 'nt' else None
        async def handle(reader, writer):
            task = asyncio.current_task()
            if len(handlers) >= 16:
                if not fatal.done():
                    fatal.set_result(ValueError("Too many concurrent emission connections"))
                writer.close()
                return
            handlers.add(task)
            clients.add(writer)
            try:
                while True:
                    if self.sandbox is not None and self.sandbox.enabled:
                        line = await reader.readline()
                    else:
                        async with asyncio.timeout(5):
                            line = await reader.readline()
                    if not line:return
                    if not line.endswith(b"\n") or len(line) > self.MAX_EVENT_BYTES + (256 if os.name == "nt" else 0):
                        raise ValueError("Malformed or oversized program event")
                    request = json.loads(line, parse_constant=_reject_nonfinite)
                    if os.name == 'nt':
                        if not isinstance(request, dict) or not secrets.compare_digest(str(request.pop('token', '')), ipc_token):
                            writer.close()
                            return
                    if not isinstance(request, dict) or set(request) != {"kind", "data"}:
                        raise ValueError("Expected kind and data")
                    if fatal.done():
                        result = {"status": "rejected", "reason": "generator_stopping"}
                    else:
                        result = await emit(request["kind"], request["data"])
                    writer.write(json.dumps(result).encode() + b"\n")
                    await writer.drain()
                    if self.sandbox is None or not self.sandbox.enabled:return
            except Exception as error:
                # Protocol faults and rate excess stop this source once, not an alarm loop.
                if not fatal.done():
                    fatal.set_result(error)
                try:
                    writer.write(json.dumps({"status": "rejected", "reason":
                        "rate_limited" if isinstance(error, ProgramRateLimited)
                        else "invalid_event"}).encode() + b"\n")
                    await writer.drain()
                except (ConnectionError, OSError):
                    pass
            finally:
                writer.close()
                clients.discard(writer)
                handlers.discard(task)

        async def log_output():
            # Retain only the current run's last 64 KiB; drain even after the cap.
            tail = b""
            with self.log_path.open("wb") as stream:
                while chunk := await self.process.stdout.read(8192):
                    tail = (tail + chunk)[-self.MAX_LOG_BYTES:]
                    stream.seek(0)
                    stream.write(tail)
                    stream.truncate()
                    stream.flush()

        with tempfile.TemporaryDirectory(prefix="harness-gen-") as ipc_dir, ExitStack() as launch_scope:
            socket_path = str(Path(ipc_dir) / "events.sock")
            try:
                if os.name == 'nt':
                    server = await asyncio.start_server(handle, '127.0.0.1', 0, limit=self.MAX_EVENT_BYTES + 256)
                    socket_path = 'tcp:127.0.0.1:' + str(server.sockets[0].getsockname()[1])
                else:
                    server = await asyncio.start_unix_server(handle, socket_path,
                                                            limit=self.MAX_EVENT_BYTES)
                env = python_environment()
                env["HARNESS_GENERATOR_SOCKET"] = socket_path
                if os.name == 'nt':
                    env['HARNESS_GENERATOR_TOKEN'] = ipc_token
                env["HARNESS_GENERATOR_STATE_DIR"] = str(self.state_dir)
                argv = [env["HARNESS_PYTHON"], "-u", self.script_path, *self.args]
                channel = None
                if getattr(self.sandbox, "windows_mxc_enabled", False):
                    env["PYTHONPATH"] = str(SDK_ROOT)
                    argv, env = launch_scope.enter_context(self.sandbox.execution(
                        argv, cwd=self.cwd, env=env, grants=[(self.state_dir, "write")],
                        explicit_env_keys=("HARNESS_GENERATOR_SOCKET", "HARNESS_GENERATOR_TOKEN",
                                           "HARNESS_GENERATOR_STATE_DIR", "PYTHONPATH"),
                    ))
                elif self.sandbox is not None and self.sandbox.enabled:
                    channel = socket.socket(socket.AF_UNIX)
                    channel.connect(socket_path)
                    env.pop('HARNESS_GENERATOR_SOCKET',None)
                    env['HARNESS_GENERATOR_FD']='0'
                    env['HARNESS_GENERATOR_STATE_DIR']='/run/aster-harness/state'
                    grant={'source':str(self.state_dir.resolve()),'target':env['HARNESS_GENERATOR_STATE_DIR'],'access':'write'}
                    argv,env=self.sandbox.command(argv,cwd=self.cwd,env=env,grants=[grant],
                        explicit_env_keys=('HARNESS_GENERATOR_FD','HARNESS_GENERATOR_STATE_DIR'))
                spawn_task = asyncio.create_task((windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(
                    *argv, cwd=self.cwd, env=env,
                    stdin=channel if channel is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT, start_new_session=True))
                if channel is not None:
                    spawn_task.add_done_callback(lambda task: channel.close())
                try:
                    self.process = await asyncio.shield(spawn_task)
                except asyncio.CancelledError:
                    # Cancellation can land after OS fork but before Python returns
                    # the handle. Recover ownership before executing cleanup.
                    self.process = await spawn_task
                    raise
                log_task = asyncio.create_task(log_output())
                wait_task = asyncio.create_task(self.process.wait())
                await asyncio.wait([wait_task, fatal, log_task], return_when=asyncio.FIRST_COMPLETED)
                if fatal.done():
                    raise fatal.result()
                if log_task.done() and log_task.exception():
                    raise log_task.exception()
                # A child may close stdout while still running. Wait without spinning.
                if not wait_task.done():
                    await asyncio.wait([wait_task, fatal], return_when=asyncio.FIRST_COMPLETED)
                if fatal.done():
                    raise fatal.result()
                code = wait_task.result()
                try:
                    await asyncio.wait_for(asyncio.shield(log_task), .5)
                except TimeoutError:
                    pass  # Cleanup kills children retaining stdout.
                if code != 0 or self.mode == "service":
                    raise RuntimeError(f"Program exited with code {code} (mode={self.mode}); "
                                       f"inspect {self.log_path}")
            finally:
                if self.process is not None:
                    # Even cancellation during spawn needs a pipe reader: otherwise
                    # wait() can hang on a dead child whose stdout buffer is full.
                    if log_task is None:
                        log_task = asyncio.create_task(log_output())
                    if wait_task is None:
                        wait_task = asyncio.create_task(self.process.wait())
                    # Kill the entire child group, including children retaining stdout.
                    if os.name == 'nt':
                        from agent_harness._windows import terminate_tree
                        await terminate_tree(self.process)
                    for sig in (() if os.name == 'nt' else (signal.SIGTERM, signal.SIGKILL)):
                        try:
                            os.killpg(self.process.pid, sig)
                        except ProcessLookupError:
                            break
                        if sig == signal.SIGTERM:
                            try:
                                await asyncio.wait_for(asyncio.shield(wait_task), .5)
                            except TimeoutError:
                                pass
                    await self.process.wait()
                # Stop the emitter before closing the listener. Let already accepted
                # transports attach before server.close (CPython accept/close race).
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                if server is not None:
                    server.close()
                    await server.wait_closed()
                for writer in list(clients):
                    writer.close()
                pending_handlers = list(handlers)
                for task in pending_handlers:
                    task.cancel()
                await asyncio.gather(*pending_handlers, return_exceptions=True)
                for task in (log_task, wait_task):
                    if task is not None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                if not fatal.done():
                    fatal.cancel()
