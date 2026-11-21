"""Native MXC acceptance with explicit paths to the signed executor and test Python."""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_harness.core.unified_exec.process_manager import UnifiedExecProcessManager
from agent_harness.experimental.windows_mxc_runtime import WindowsMxcRuntime


@unittest.skipUnless(os.name == "nt", "native Windows MXC required")
class WindowsMxcRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        executor = os.environ.get("ASTER_MXC_TEST_EXECUTOR")
        python = os.environ.get("ASTER_MXC_TEST_PYTHON")
        if not executor or not python:
            self.skipTest("Set ASTER_MXC_TEST_EXECUTOR and ASTER_MXC_TEST_PYTHON")
        self.shell = Path(os.environ.get("ASTER_MXC_TEST_POWERSHELL") or (
            Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        ))
        self.workspace = Path(self.enterContext(TemporaryDirectory(prefix="aster-mxc-runtime-test-"))).resolve()
        private = self.workspace / "private"
        private.mkdir()
        (private / "secret.txt").write_text("synthetic secret", encoding="utf-8")
        self.runtime = WindowsMxcRuntime(
            executor, self.workspace, {"rules": []},
            python=python, powershell=self.shell, private_roots=[private],
        )
        self.manager = UnifiedExecProcessManager(self.workspace, self.runtime)
        self.addAsyncCleanup(self.manager.close)

    async def test_unified_exec_and_file_worker_share_isolation(self):
        result = await self.manager.exec_command(
            "[IO.File]::WriteAllText('first.txt', '中文'); "
            "try { [IO.File]::ReadAllText('private/secret.txt') } catch { 'PRIVATE_DENIED' }",
            shell=str(self.shell),
        )
        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertEqual(result["output"].strip(), "PRIVATE_DENIED")
        self.assertEqual(
            self.runtime.file_operation("read", path="first.txt", start_line=1, end_line=1),
            "1: 中文",
        )
        with self.assertRaisesRegex(ValueError, "PermissionError"):
            self.runtime.file_operation("read", path="private/secret.txt", start_line=1, end_line=1)
        self.runtime.file_operation("patch", patch="*** Begin Patch\n*** Add File: patched.txt\n+中文补丁\n*** End Patch")
        result = await self.manager.exec_command("Get-Content patched.txt -Encoding UTF8", shell=str(self.shell))
        self.assertEqual(result.get("exit_code"), 0, result)
        self.assertEqual(result["output"].strip(), "中文补丁")

    async def test_background_output_and_stop_reap_python_child(self):
        import win32api
        import win32event

        result = await self.manager.exec_command(
            "& $env:HARNESS_PYTHON -I -c \"import os,time; print('child:'+str(os.getpid()),flush=True); time.sleep(90)\"",
            shell=str(self.shell), yield_time_ms=1500,
        )
        self.assertIn("session_id", result, result)
        session = result["session_id"]
        if "child:" not in result["output"]:
            result = await self.manager.write_stdin(session, yield_time_ms=5000)
        match = re.search(r"child:(\d+)", result["output"])
        self.assertIsNotNone(match, result)
        child = win32api.OpenProcess(0x100000, False, int(match[1]))  # SYNCHRONIZE
        try:
            stopped = await self.manager.stop_process(session)
            self.assertEqual(stopped["status"], "stopped", stopped)
            self.assertEqual(win32event.WaitForSingleObject(child, 3000), 0)
        finally:
            child.Close()

    async def test_session_policy_reconnects_mcp_and_restarts_program(self):
        import win32api
        import win32event
        from agent_harness.core.client import ScriptedClient
        from agent_harness.core.event_generators.program import ProgramGenerator
        from agent_harness.core.session import SessionConfiguration
        from agent_harness.core.thread_manager import ThreadManager

        root = Path(self.enterContext(TemporaryDirectory(prefix="aster-mxc-session-"))).resolve()
        outside = root / "outside.txt"
        outside.write_text("outside fixture", encoding="utf-8")
        mcp = self.workspace / "server.py"
        mcp.write_text(
            "from mcp.server.fastmcp import FastMCP\nfrom pathlib import Path\nimport os\n"
            "m=FastMCP('native')\n@m.tool()\ndef probe(path:str)->str:\n"
            " try: access=Path(path).read_text()\n except OSError: access='blocked'\n"
            " return str(os.getpid())+':'+access\nm.run(transport='stdio')\n",
            encoding="utf-8",
        )
        program = self.workspace / "source.py"
        program.write_text(
            "from agent_harness.events import emit_event,state_directory\nfrom pathlib import Path\n"
            "import os,sys,time\np=state_directory()/'count'\n"
            "count=int(p.read_text())+1 if p.exists() else 1\np.write_text(str(count))\n"
            "try: result=Path(sys.argv[1]).read_text()\nexcept OSError: result='blocked'\n"
            "assert emit_event('probe',{'count':count,'access':result,'pid':os.getpid()})['status']=='accepted'\n"
            "time.sleep(90)\n", encoding="utf-8",
        )
        responses = [[{"type": "function_call", "name": "wait_for_event",
                       "arguments": "{}", "call_id": f"wait-{index}"}] for index in range(8)]
        events = asyncio.Queue()

        def trace(name, data):
            if name == "generator_fired":
                events.put_nowait(data["generated_event"])

        with patch.dict(os.environ, {"ASTER_MXC_EXECUTOR": str(self.runtime.executor)}):
            session = ThreadManager(root / "threads").create(
                ScriptedClient(responses), self.workspace, trace=trace,
                configuration=SessionConfiguration(
                    filesystem_policy={"rules": [{"path": "/", "access": "read"}]},
                    mcp_servers={"probe": {"command": sys.executable, "args": [str(mcp)], "required": True}},
                ),
            )
            self.addAsyncCleanup(session.close)
            await session._prepare_mcp_tools()
            before = dict(session.services.mcp.binding.servers)["probe"]
            result = await before.call_tool("probe", {"path": str(outside)})
            pid, access = result["structuredContent"]["result"].split(":", 1)
            self.assertEqual(access, "outside fixture")
            old_mcp = win32api.OpenProcess(0x100000, False, int(pid))
            self.addCleanup(old_mcp.Close)
            session.generator_manager.register(ProgramGenerator(
                str(program), str(self.workspace), args=[str(outside)], mode="service",
            ))
            first = (await asyncio.wait_for(events.get(), 10))["data"]["payload"]
            self.assertEqual((first["count"], first["access"]), (1, "outside fixture"))
            old_program = win32api.OpenProcess(0x100000, False, first["pid"])
            self.addCleanup(old_program.Close)
            await session.set_filesystem_policy({"rules": []})
            await session.run_until_idle()
            self.assertEqual(session.sandbox.version, 2)
            self.assertEqual(win32event.WaitForSingleObject(old_mcp, 0), 0)
            self.assertEqual(win32event.WaitForSingleObject(old_program, 0), 0)
            after = dict(session.services.mcp.binding.servers)["probe"]
            self.assertIsNot(before, after)
            result = await after.call_tool("probe", {"path": str(outside)})
            self.assertIn("blocked", json.dumps(result))
            event = await asyncio.wait_for(events.get(), 10)
            self.assertIn("payload", event["data"], repr(event))
            second = event["data"]["payload"]
            self.assertEqual((second["count"], second["access"]), (2, "blocked"))
            await session.close()
