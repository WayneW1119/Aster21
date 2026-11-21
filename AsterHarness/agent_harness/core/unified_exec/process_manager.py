"""Session-owned process handles; yielding a tool result does not kill a command."""

import asyncio
import os
if os.name == "nt":
    from agent_harness._windows import spawn as windows_spawn
import secrets
import time
from pathlib import Path
from uuid import uuid4

from .process import UnifiedExecProcess
from ..runtime_environment import python_environment


DEFAULT_SHELL = '/bin/bash'
if os.name == 'nt':
    import shutil
    DEFAULT_SHELL = shutil.which('pwsh.exe') or shutil.which('powershell.exe') or os.environ['COMSPEC']


def integer(name: str, value: int, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


class UnifiedExecProcessManager:
    def __init__(self, workspace: Path, sandbox=None):
        self.sandbox = sandbox
        self.workspace = workspace.resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be a directory")
        self.processes: dict[int, UnifiedExecProcess] = {}
        self.closed = False
        self._lifecycle = asyncio.Lock()

    async def exec_command(self, cmd: str, workdir: str | None = None,
                           yield_time_ms: int = 10000, max_output_tokens: int = 10000,
                           shell: str = DEFAULT_SHELL, login: bool = True,
                           tty: bool = False) -> dict:
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("cmd must be a non-empty string")
        if os.name == 'nt' and tty:
            raise ValueError('Windows preview does not yet support tty=true; use non-TTY commands')
        if os.name == 'nt' and shell == DEFAULT_SHELL:
            shell = getattr(self.sandbox, "windows_mxc_shell", None) or shell
        if not isinstance(shell, str) or not Path(shell).is_absolute():
            raise ValueError("shell must be an absolute executable path")
        if type(login) is not bool or type(tty) is not bool:
            raise ValueError("login and tty must be booleans")
        wait_ms = max(250, min(30000, integer("yield_time_ms", yield_time_ms)))
        budget = min(100000, integer("max_output_tokens", max_output_tokens, 1))
        if workdir is not None and not isinstance(workdir, str):
            raise ValueError("workdir must be a string")
        cwd = (self.workspace / (workdir or ".")).resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError("workdir must be a directory")
        start = time.monotonic()
        async with self._lifecycle:
            if self.closed:
                raise ValueError("Process manager is closed")
            if len(self.processes) >= 64:
                raise ValueError("Process handle limit reached; poll finished commands first")
            handle = secrets.randbelow(99000) + 1000
            while handle in self.processes:
                handle = secrets.randbelow(99000) + 1000
            master = slave = None
            try:
                if tty:
                    master, slave = os.openpty()
                    os.set_blocking(master, False)
                argv, env = [shell, "-lc" if login else "-c", cmd], python_environment()
                if os.name == 'nt':
                    from agent_harness._windows import shell_arguments
                    argv = shell_arguments(shell, cmd, login)
                    env['PYTHONIOENCODING'] = 'utf-8'
                sandbox_spawn = getattr(self.sandbox, 'spawn_windows_exec', None) if os.name == 'nt' else None
                if sandbox_spawn is not None:
                    launch = asyncio.create_task(sandbox_spawn(cmd, cwd=cwd, shell=shell))
                else:
                    if self.sandbox is not None:
                        argv, env = self.sandbox.command(argv, cwd=cwd, env=env)
                    launch = asyncio.create_task((windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(
                        *argv, cwd=cwd, env=env,
                        stdin=slave if tty else asyncio.subprocess.DEVNULL,
                        stdout=slave if tty else asyncio.subprocess.PIPE,
                        stderr=slave if tty else asyncio.subprocess.STDOUT,
                        start_new_session=True))
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    # Spawn may already have crossed the OS boundary. Finish obtaining
                    # its handle and reap it instead of orphaning an unregistered child.
                    process = await launch
                    if slave is not None:
                        os.close(slave)
                        slave = None
                    entry = UnifiedExecProcess(process, master)
                    master = None  # ownership transferred to entry
                    await entry.close()
                    raise
            except BaseException:
                if master is not None:
                    os.close(master)
                raise
            finally:
                if slave is not None:
                    os.close(slave)
            entry = UnifiedExecProcess(process, master, command=cmd, cwd=str(cwd))
            self.processes[handle] = entry
        async with entry.interaction_lock:
            return await self._collect(handle, entry, wait_ms, budget, start)

    async def write_stdin(self, session_id: int, chars: str = "",
                          yield_time_ms: int | None = None,
                          max_output_tokens: int = 10000) -> dict:
        integer("session_id", session_id, 1)
        if not isinstance(chars, str):
            raise ValueError("chars must be a string")
        if len(chars.encode()) > 65536:
            raise ValueError("stdin input is limited to 65536 bytes per call")
        wait = (250 if chars else 5000) if yield_time_ms is None else integer("yield_time_ms", yield_time_ms)
        wait = max(250 if chars else 5000, min(30000 if chars else 300000, wait))
        budget = min(100000, integer("max_output_tokens", max_output_tokens, 1))
        if self.closed:
            raise ValueError("Process manager is closed")
        entry = self.processes.get(session_id)
        if entry is None:
            raise ValueError(f"Unknown process session_id: {session_id}")
        start = time.monotonic()
        async with entry.interaction_lock:
            if session_id not in self.processes:
                raise ValueError(f"Unknown process session_id: {session_id}")
            await entry.write(chars)
            return await self._collect(session_id, entry, wait, budget, start)

    async def _collect(self, handle, entry, wait, budget, start) -> dict:
        result = await entry.collect(wait, budget)
        result.update(chunk_id=uuid4().hex[:8], wall_time_seconds=round(time.monotonic() - start, 4))
        if "exit_code" in result:
            self.processes.pop(handle, None)
            await entry.close()
        else:
            result["session_id"] = handle
        return result

    async def list_processes(self) -> list[dict]:
        """Only handles owned by this live Session, including host-started commands.

        Listing does not consume output or remove completed, uncollected entries.
        Host PIDs are deliberately not accepted as capabilities.
        """
        async with self._lifecycle:
            if self.closed:
                raise ValueError("Process manager is closed")
            return [{"session_id": handle, "command": entry.command,
                     "workdir": entry.cwd, "tty": entry.tty,
                     "status": "running" if entry.process.returncode is None else "exited",
                     "exit_code": entry.process.returncode,
                     "output_pending": not entry.complete.is_set() or bool(entry.chunks)}
                    for handle, entry in sorted(self.processes.items())]

    async def stop_process(self, session_id: int, max_output_tokens: int = 10000) -> dict:
        integer("session_id", session_id, 1)
        budget = min(100000, integer("max_output_tokens", max_output_tokens, 1))
        if self.closed:
            raise ValueError("Process manager is closed")
        entry = self.processes.get(session_id)
        if entry is None:
            raise ValueError(f"Unknown process session_id: {session_id}")
        start = time.monotonic()
        already_exited = entry.process.returncode is not None
        # Interrupt an outstanding long poll without waiting for its interaction
        # lock. close() is idempotent; output is still consumed under that lock.
        await entry.close()
        async with entry.interaction_lock:
            if entry.process.returncode is None:
                return {"status": "termination_pending", "session_id": session_id,
                        "note": "Termination was requested but exit is not confirmed."}
            if self.processes.get(session_id) is entry:
                result = await self._collect(session_id, entry, 0, budget, start)
            else:
                # A concurrent poll or shutdown already reaped this exact entry.
                result = {"output": "", "exit_code": entry.process.returncode}
            result.update(status="already_exited" if already_exited else "stopped",
                          stopped_session_id=session_id)
            return result

    async def terminate_all(self):
        async with self._lifecycle:
            handles = list(self.processes)
            entries = list(self.processes.values())
            self.processes.clear()
        await asyncio.gather(*(entry.close() for entry in entries))
        return handles

    async def close(self) -> None:
        async with self._lifecycle:
            self.closed = True
            entries = list(self.processes.values())
            self.processes.clear()
        await asyncio.gather(*(entry.close() for entry in entries))
