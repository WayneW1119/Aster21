"""One UAC-approved Squad worker, connected to its ordinary controller."""

import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory

from ._windows import private_file


def is_admin():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _proof(key, role, nonce):
    return hmac.digest(key, role + nonce, hashlib.sha256).hex().encode() + b"\n"


def _launch(request):
    import pythoncom
    import win32con
    from win32com.shell import shell, shellcon

    pythonw = Path(sys.executable).with_name("pythonw.exe").resolve(strict=True)
    pythoncom.CoInitialize()
    try:
        result = shell.ShellExecuteEx(
            # PyWin32 exposes SEE_MASK_NOASYNC by its older alias.
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_FLAG_DDEWAIT,
            lpVerb="runas", lpFile=str(pythonw),
            lpParameters=subprocess.list2cmdline(["-I", "-m", "squad.windows_admin", str(request)]),
            lpDirectory=os.environ["SystemRoot"], nShow=win32con.SW_HIDE,
        )
        return result["hProcess"]
    finally:
        pythoncom.CoUninitialize()


class ElevatedWorker:
    def __init__(self, handle, reader, writer):
        self.stdout, self.stdin = reader, writer
        self.returncode = None
        self._exit = asyncio.create_task(asyncio.to_thread(self._wait, handle))

    def _wait(self, handle):
        import win32event
        import win32process

        try:
            win32event.WaitForSingleObject(handle, win32event.INFINITE)
            return win32process.GetExitCodeProcess(handle)
        finally:
            handle.Close()

    async def wait(self):
        self.returncode = await asyncio.shield(self._exit)
        self.stdin.close()
        return self.returncode


async def spawn_admin(config, env):
    key = secrets.token_bytes(32)
    connected = asyncio.get_running_loop().create_future()

    async def authenticate(reader, writer):
        accepted = False
        try:
            async with asyncio.timeout(5):
                nonce = await reader.readline()
                if len(nonce) != 65 or not nonce.endswith(b"\n"):
                    return
                writer.write(_proof(key, b"controller", nonce))
                await writer.drain()
                response = await reader.readline()
                if not hmac.compare_digest(response, _proof(key, b"worker", nonce)) or connected.done():
                    return
                accepted = True
                connected.set_result((reader, writer))
        except (OSError, TimeoutError, ValueError):
            pass
        finally:
            if not accepted:
                writer.close()

    server = await asyncio.start_server(authenticate, "127.0.0.1", 0, limit=256 * 1024)
    handle = None
    attached = False
    try:
        # Keep the launch secret under the same protected controller tree as worker.json.
        with TemporaryDirectory(prefix="admin-", dir=config.parent) as directory:
            request = Path(directory) / "launch.json"
            request.touch(exist_ok=False)
            private_file(request)
            request.write_text(json.dumps({
                "port": server.sockets[0].getsockname()[1], "key": key.hex(), "config": str(config),
            }), encoding="utf-8")
            launch = asyncio.create_task(asyncio.to_thread(_launch, request))
            try:
                handle = await asyncio.shield(launch)
            except asyncio.CancelledError:
                handle = await launch
                raise
            reader, writer = await asyncio.wait_for(asyncio.shield(connected), 12)
            writer.write(json.dumps(env).encode("utf-8") + b"\n")
            await writer.drain()
            process = ElevatedWorker(handle, reader, writer)
            attached = True
            return process
    finally:
        server.close()
        if not attached:
            if connected.done() and not connected.cancelled():
                connected.result()[1].close()
            else:
                connected.cancel()
            if handle is not None:
                handle.Close()
            await server.wait_closed()


def main():
    from . import worker
    from ._windows import is_reparse

    if not is_admin() or len(sys.argv) != 2:
        raise RuntimeError("A UAC-approved administrator token is required")
    request = Path(sys.argv[1])
    if not request.is_absolute() or any(is_reparse(p) for p in (request, *request.parents)):
        raise RuntimeError("Invalid administrator launch request")
    data = json.loads(request.read_text(encoding="utf-8"))
    config = Path(data["config"])
    if config != request.parent.parent / "worker.json" or is_reparse(config):
        raise RuntimeError("Invalid administrator worker configuration")
    key = bytes.fromhex(data["key"])
    nonce = secrets.token_hex(32).encode() + b"\n"
    with socket.create_connection(("127.0.0.1", data["port"]), timeout=10) as connection:
        with connection.makefile("rb") as source, connection.makefile("wb") as output:
            output.write(nonce)
            output.flush()
            if not hmac.compare_digest(source.readline(66), _proof(key, b"controller", nonce)):
                raise RuntimeError("Controller authentication failed")
            output.write(_proof(key, b"worker", nonce))
            output.flush()
            environment = json.loads(source.readline(256 * 1024))
            os.environ.clear()
            os.environ.update(environment)
            connection.settimeout(None)
            import io
            log = config.with_name("worker.log")
            if is_reparse(log):
                raise RuntimeError("Invalid administrator worker log path")
            previous = sys.stdin, sys.stdout, sys.stderr
            with (io.TextIOWrapper(source, encoding="utf-8") as stdin,
                  io.TextIOWrapper(output, encoding="utf-8", write_through=True) as stdout,
                  log.open("a", encoding="utf-8") as stderr):
                sys.stdin, sys.stdout, sys.stderr = stdin, stdout, stderr
                try:
                    sys.argv = ["squad.worker", "--config", str(config)]
                    worker.main()
                finally:
                    # Unblock stdin if the worker failed while a read was pending.
                    with contextlib.suppress(OSError):
                        connection.shutdown(socket.SHUT_RD)
                    sys.stdin, sys.stdout, sys.stderr = previous


if __name__ == "__main__":
    main()
