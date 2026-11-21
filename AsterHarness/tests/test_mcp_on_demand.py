"""Stateless stdio tools release idle children without changing call semantics."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from agent_harness.core.mcp.client import create_mcp_client, McpCallError, OnDemandMcpClient
from agent_harness.core.mcp.config import McpServerConfig

FIXTURE = Path(__file__).parent / "fixtures/mcp_server.py"


class OnDemandConfigTests(unittest.TestCase):
    def test_explicit_stdio_only_opt_in(self):
        self.assertEqual(McpServerConfig(command="python").connection_mode, "persistent")
        self.assertNotIn("connection_mode", McpServerConfig(command="python").to_dict())
        for kwargs in (
            {"command": "python", "connection_mode": "lazy"},
            {"command": "python", "connection_mode": True},
            {"url": "http://localhost/mcp", "connection_mode": "on_demand"},
        ):
            with self.assertRaises(ValueError):
                McpServerConfig(**kwargs)
        value = McpServerConfig(command="python", connection_mode="on_demand")
        self.assertEqual(McpServerConfig(**value.to_dict()), value)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP SDK required")
class OnDemandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = McpServerConfig(
            command=sys.executable,
            args=(str(FIXTURE.resolve()), "--state-dir", str(self.root / "state")),
            connection_mode="on_demand",
            tool_timeout_sec=2,
        )
        self.client = create_mcp_client(self.config, self.root, self.root / "stderr.log")
        self.addAsyncCleanup(self.client.close)

    def effects(self):
        file = self.root / "state/effects.jsonl"
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def assert_children_gone(self):
        for row in self.effects():
            if row["kind"] == "started":
                if os.name == "nt":
                    # Unlike POSIX, Windows os.kill(pid, 0) may terminate a process.
                    import ctypes
                    from ctypes import wintypes

                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.GetExitCodeProcess.argtypes = [
                        wintypes.HANDLE,
                        ctypes.POINTER(wintypes.DWORD),
                    ]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    handle = kernel.OpenProcess(0x1000, False, row["pid"])
                    if handle:
                        try:
                            code = wintypes.DWORD()
                            self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                            self.assertNotEqual(code.value, 259)  # STILL_ACTIVE
                        finally:
                            kernel.CloseHandle(handle)
                else:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(row["pid"], 0)

    async def test_discovery_and_each_call_leave_no_child_and_keep_tools_resources(self):
        self.assertIsInstance(self.client, OnDemandMcpClient)
        tools = await self.client.start()
        self.assert_children_gone()
        self.assertTrue(self.client.has_resources)
        self.assertTrue(self.client.instructions)
        results = await asyncio.gather(
            *(self.client.call_tool("send_message", {"text": value}) for value in ("one", "two"))
        )
        self.assertTrue(all(not r.get("isError") for r in results))
        await self.client.request("resources/read", {"uri": "fixture://guide"})
        self.assert_children_gone()
        self.assertEqual([r["text"] for r in self.effects() if r["kind"] == "sent"], ["one", "two"])
        self.assertEqual(len([r for r in self.effects() if r["kind"] == "started"]), 4)
        tools.clear()
        self.assertTrue(await self.client.start())
        self.assertEqual(len([r for r in self.effects() if r["kind"] == "started"]), 4)

    async def test_crash_and_timeout_do_not_retry_side_effects(self):
        await self.client.start()
        for name in ("crash", "slow"):
            with self.subTest(name=name), self.assertRaises(McpCallError) as error:
                await self.client.call_tool(name, {})
            self.assertTrue(error.exception.outcome_unknown)
            self.assertEqual(sum(row["kind"] == name for row in self.effects()), 1)
            self.assert_children_gone()

    async def test_catalog_change_blocks_before_business_call(self):
        script = self.root / "server.py"
        script.write_bytes(FIXTURE.read_bytes())
        config = McpServerConfig(
            command=sys.executable,
            args=(str(script), "--state-dir", str(self.root / "state")),
            connection_mode="on_demand",
        )
        client = create_mcp_client(config, self.root, self.root / "drift.log")
        self.addAsyncCleanup(client.close)
        await client.start()
        script.write_text(
            script.read_text().replace("def send_message(text:", "def changed_send_message(text:")
        )
        with self.assertRaises(McpCallError) as error:
            await client.call_tool("send_message", {"text": "never sent"})
        self.assertFalse(error.exception.outcome_unknown)
        self.assertFalse(any(row["kind"] == "sent" for row in self.effects()))
        self.assert_children_gone()

    async def test_unavailable_server_after_discovery_does_not_dispatch(self):
        script = self.root / "server.py"
        script.write_bytes(FIXTURE.read_bytes())
        config = McpServerConfig(
            command=sys.executable,
            args=(str(script), "--state-dir", str(self.root / "state")),
            connection_mode="on_demand",
        )
        client = create_mcp_client(config, self.root, self.root / "unavailable.log")
        self.addAsyncCleanup(client.close)
        await client.start()
        script.unlink()
        with self.assertRaises(McpCallError) as error:
            await client.call_tool("send_message", {"text": "never sent"})
        self.assertFalse(error.exception.outcome_unknown)
        self.assertFalse(any(row["kind"] == "sent" for row in self.effects()))
        self.assert_children_gone()

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/usr/bin/bwrap").exists(),
        "Linux sandbox required",
    )
    async def test_on_demand_calls_keep_the_callers_filesystem_sandbox(self):
        from agent_harness.core.sandbox import FilesystemSandbox

        secret = self.root / "secret.txt"
        secret.write_text("PRIVATE_VALUE_NEVER_RETURN")
        script = self.root / "sandbox-server.py"
        script.write_text(
            FIXTURE.read_text().replace(
                '    server.run(transport="stdio")',
                "    @server.tool()\n    def read_secret() -> str:\n        return Path("
                + repr(str(secret))
                + ').read_text()\n\n    server.run(transport="stdio")',
            )
        )
        config = McpServerConfig(
            command=sys.executable,
            args=(str(script), "--state-dir", str(self.root / "state")),
            connection_mode="on_demand",
        )
        client = create_mcp_client(config, self.root, self.root / "sandbox.log")
        self.addAsyncCleanup(client.close)
        client.sandbox = FilesystemSandbox(
            self.root,
            {"rules": [{"path": "/", "access": "read"}, {"path": str(secret), "access": "deny"}]},
        )
        await client.start()
        for _ in range(2):
            result = await client.call_tool("read_secret", {})
            self.assertTrue(result.get("isError"))
            self.assertNotIn("PRIVATE_VALUE_NEVER_RETURN", json.dumps(result))
        # Sandboxed PIDs belong to a private namespace (the server reports 1).
        # Check its unique executable argument in the host namespace instead.
        for command in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                args = command.read_bytes().split(b"\0")
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            self.assertNotIn(os.fsencode(script), args)

    async def test_close_interrupts_active_call_and_refuses_queued_call(self):
        await self.client.start()
        active = asyncio.create_task(self.client.call_tool("slow", {}))
        queued = asyncio.create_task(self.client.call_tool("send_message", {"text": "never sent"}))
        for _ in range(100):
            if any(row["kind"] == "slow" for row in self.effects()):
                break
            await asyncio.sleep(0.05)
        else:
            self.fail("fixture call was not dispatched")
        await self.client.close()
        results = await asyncio.gather(active, queued, return_exceptions=True)
        self.assertTrue(all(isinstance(r, McpCallError) for r in results))
        self.assertTrue(results[0].outcome_unknown)
        self.assertFalse(results[1].outcome_unknown)
        self.assertFalse(any(row["kind"] == "sent" for row in self.effects()))
        self.assert_children_gone()
