"""Bounded JSON-stdin script execution; ordinary host permissions, no shell."""

import asyncio
import os
if os.name == "nt":
    from agent_harness._windows import spawn as windows_spawn
import json
import os
import signal
from contextlib import ExitStack
from pathlib import Path

from ..runtime_environment import python_environment


async def run_script(command, script, args, cwd, arguments, timeout_sec, sandbox=None):
    payload = (json.dumps(arguments, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(payload) > 1024 * 1024:
        raise ValueError("Custom tool JSON input exceeds 1 MiB")
    argv, env = [command, script, *args], python_environment()
    with ExitStack() as launch_scope:
        if getattr(sandbox, "windows_mxc_enabled", False):
            argv, env = launch_scope.enter_context(sandbox.execution(
                argv, cwd=cwd, env=env, grants=[(script, "read")],
            ))
        elif sandbox is not None and sandbox.enabled:
            grant = {'source':str(Path(script).resolve()), 'target':'/run/aster-harness/tool.py','access':'read'}
            argv, env = sandbox.command([command,grant['target'],*args],cwd=cwd,env=env,grants=[grant])
        return await _run(argv, env, cwd, payload, timeout_sec)


async def _run(argv, env, cwd, payload, timeout_sec):
    # Limit retained output while continuing to drain both pipes to avoid deadlock.
    async def collect(stream):
        result = bytearray()
        truncated = False
        while data := await stream.read(8192):
            remaining = 65536 - len(result)
            result.extend(data[:remaining])
            truncated |= len(data) > remaining
        return result.decode("utf-8", errors="replace"), truncated

    launch = asyncio.create_task((windows_spawn if os.name == 'nt' else asyncio.create_subprocess_exec)(
        *argv, cwd=cwd, env=env, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
    process = None
    tasks = []
    timed_out = False
    try:
        process = await asyncio.shield(launch)
        tasks = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]

        async def feed_and_wait():
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            await process.wait()
            await asyncio.gather(*(asyncio.shield(task) for task in tasks))

        try:
            await asyncio.wait_for(feed_and_wait(), timeout_sec)
        except TimeoutError:
            timed_out = True
    finally:
        if process is None:
            # Cancellation may arrive after the OS spawn but before its handle is returned.
            process = await launch
        if os.name == 'nt':
            from agent_harness._windows import terminate_tree
            await terminate_tree(process)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()
        if tasks:
            output = await asyncio.gather(*tasks)
        else:
            await process.communicate()
    (stdout, stdout_truncated), (stderr, stderr_truncated) = output
    return {"success": not timed_out and process.returncode == 0,
            "exit_code": process.returncode, "timed_out": timed_out,
            "stdout": stdout, "stderr": stderr,
            "stdout_truncated": stdout_truncated, "stderr_truncated": stderr_truncated,
            "outcome_unknown": timed_out,
            "note": "No automatic retry; verify side effects before repeating a failed or timed-out call."}
