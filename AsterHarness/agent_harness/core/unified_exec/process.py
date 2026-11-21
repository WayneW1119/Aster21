"""One live command and bounded, incrementally consumed output (Unix)."""

import asyncio
import codecs
import errno
import os
import signal
from collections import deque


class UnifiedExecProcess:
    MAX_BUFFER_BYTES = 1024 * 1024

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int | None,
                 *, command: str = "", cwd: str = ""):
        self.process = process
        self.master_fd = master_fd
        self.command = command
        self.cwd = cwd
        self.tty = master_fd is not None
        self._close_task = None
        self.chunks: deque[bytes] = deque()
        self.buffer_bytes = 0
        self.omitted_bytes = 0
        self.complete = asyncio.Event()
        self.output_closed = asyncio.Event()
        self.interaction_lock = asyncio.Lock()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.monitor = asyncio.create_task(self._monitor())

    def _append(self, chunk: bytes) -> None:
        self.chunks.append(chunk)
        self.buffer_bytes += len(chunk)
        while self.buffer_bytes > self.MAX_BUFFER_BYTES:
            excess = self.buffer_bytes - self.MAX_BUFFER_BYTES
            first = self.chunks.popleft()
            removed = min(excess, len(first))
            self.omitted_bytes += removed
            self.buffer_bytes -= removed
            if removed < len(first):
                self.chunks.appendleft(first[removed:])

    async def _monitor(self) -> None:
        try:
            if self.master_fd is None:
                while chunk := await self.process.stdout.read(8192):
                    self._append(chunk)
                self.output_closed.set()
            else:
                loop = asyncio.get_running_loop()
                def readable() -> None:
                    try:
                        chunk = os.read(self.master_fd, 8192)
                    except OSError as error:
                        if error.errno == errno.EAGAIN:
                            return
                        chunk = b""
                    if chunk:
                        self._append(chunk)
                    else:
                        loop.remove_reader(self.master_fd)
                        self.output_closed.set()
                loop.add_reader(self.master_fd, readable)
                try:
                    await self.output_closed.wait()
                finally:
                    loop.remove_reader(self.master_fd)
            await self.process.wait()
        finally:
            self.complete.set()

    async def collect(self, wait_ms: int, max_output_tokens: int) -> dict:
        try:
            await asyncio.wait_for(self.complete.wait(), wait_ms / 1000)
        except TimeoutError:
            pass
        content = b"".join(self.chunks)
        omitted = self.omitted_bytes
        self.chunks.clear()
        self.buffer_bytes = self.omitted_bytes = 0
        # Approximate token budget, not a tokenizer. Keep both ends of this chunk.
        budget = max_output_tokens * 4
        if len(content) > budget:
            omitted += len(content) - budget
            half = budget // 2
            content = content[:half] + b"\n[output truncated]\n" + content[-half:]
        if omitted:
            self.decoder.reset()
        text = self.decoder.decode(content, final=self.complete.is_set())
        if omitted:
            text = f"[output truncated; {omitted} bytes omitted]\n" + text
        result = {"output": text}
        if self.complete.is_set():
            # Surface unexpected reader errors instead of silently losing output.
            await self.monitor
            result["exit_code"] = self.process.returncode
        return result

    async def write(self, text: str) -> None:
        if not text:
            return
        if self.complete.is_set() or self.process.returncode is not None:
            raise ValueError("Process has exited; cannot write input")
        if os.name == 'nt' and text == "\x03":
            from agent_harness._windows import terminate_tree
            await terminate_tree(self.process)
            return
        if text == "\x03":
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass  # Exit may race the interruption; collect its final output.
            return
        if self.master_fd is None:
            raise ValueError("stdin is unavailable; start exec_command with tty=true")
        data = text.encode()
        while data:
            try:
                written = os.write(self.master_fd, data)
                data = data[written:]
            except BlockingIOError:
                loop = asyncio.get_running_loop()
                ready = loop.create_future()
                def writable() -> None:
                    if not ready.done():
                        ready.set_result(None)
                loop.add_writer(self.master_fd, writable)
                try:
                    await ready
                finally:
                    loop.remove_writer(self.master_fd)

    async def close(self) -> None:
        # Polling, explicit stop and Session shutdown may race. One owned cleanup
        # must finish even if its first caller is cancelled.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if os.name == 'nt':
            from agent_harness._windows import terminate_tree
            await terminate_tree(self.process)
            try:
                await asyncio.wait_for(asyncio.shield(self.monitor), 3)
            except TimeoutError:
                self.monitor.cancel()
                await asyncio.gather(self.monitor, return_exceptions=True)
            return
        # Commands get their own process group; clean up children as well as shell.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(self.monitor), 0.5)
        except TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(self.monitor), 1)
            except TimeoutError:
                self.monitor.cancel()
                await asyncio.gather(self.monitor, return_exceptions=True)
        finally:
            if self.master_fd is not None:
                os.close(self.master_fd)
                self.master_fd = None
