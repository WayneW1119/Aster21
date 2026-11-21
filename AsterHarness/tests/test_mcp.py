"""Real SDK subprocess integration and stdlib-only configuration regressions."""

import asyncio
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_harness.core.client import ScriptedClient
from agent_harness.core.mcp.binding import model_tool_name
from agent_harness.core.mcp.config import McpServerConfig, load_mcp_config
from agent_harness.core.mcp.client import McpCallError
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.trace import JsonlTrace


SDK_AVAILABLE = importlib.util.find_spec("mcp") is not None
FIXTURE = Path(__file__).parent / "fixtures/mcp_server.py"
RUN_ROOT = None


def setUpModule():
    global RUN_ROOT
    RUN_ROOT = Path(__file__).resolve().parents[1] / "test_runs" / ("mcp-" + uuid4().hex[:12])
    RUN_ROOT.mkdir(parents=True)
    print("MCP regression artifacts:", RUN_ROOT, flush=True)


def call(name, identifier, **arguments):
    return {"type": "function_call", "name": name, "call_id": identifier,
            "arguments": json.dumps(arguments, ensure_ascii=False)}


def wait(identifier="wait"):
    return call("wait_for_event", identifier)


class McpConfigurationTests(unittest.TestCase):
    def test_validation_and_detached_configuration(self):
        raw = {"demo": {"command": "python", "args": ["server.py"]}}
        config = SessionConfiguration(mcp_servers=raw)
        raw["demo"]["args"].clear()
        self.assertEqual(config.mcp_servers["demo"]["args"], ["server.py"])
        for value in ({"bad.name": {"command": "python"}},
                {"demo": {"url": "ftp://localhost/mcp"}},
                {"demo": {"command": ""}}, {"demo": {"command": "python", "args": "server.py"}},
                {"demo": {"command": "python", "startup_timeout_sec": True}},
                {"demo": {"command": "python", "tool_timeout_sec": float("nan")}},
                {"demo": {"command": "python", "env_vars": ["A=B"]}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionConfiguration(mcp_servers=value)

    def test_toml_matches_codex_style_and_names_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp.toml"
            path.write_text('[mcp_servers.demo]\ncommand="python"\nargs=["server.py"]\n')
            self.assertEqual(load_mcp_config(path)["demo"]["command"], "python")
        self.assertEqual(model_tool_name("demo", "send_message"), "mcp__demo__send_message")
        for original in ("a.b", "a/b", "x" * 100):
            name = model_tool_name("demo", original)
            self.assertLessEqual(len(name), 64)
            self.assertRegex(name, r"^[A-Za-z0-9_-]+$")
        self.assertNotEqual(model_tool_name("demo", "a.b"), model_tool_name("demo", "a/b"))

    def test_mcp_is_optional_for_core_imports(self):
        project = Path(__file__).resolve().parents[1]
        code = '''
import sys
class BlockMcp:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ModuleNotFoundError("mcp intentionally unavailable")
sys.meta_path.insert(0, BlockMcp())
from agent_harness.core.session import SessionConfiguration
assert SessionConfiguration().mcp_servers == {}
from agent_harness.core.mcp.client import require_sdk
try:
    require_sdk()
except RuntimeError as error:
    assert '.[mcp]' in str(error)
else:
    raise AssertionError("Dependency check missing")
'''
        process = subprocess.run([sys.executable, "-c", code], cwd=project,
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)


@unittest.skipUnless(SDK_AVAILABLE, "Install .[mcp] for real stdio MCP integration")
class McpIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = []
        self.trace_file = JsonlTrace(RUN_ROOT / (self._testMethodName + ".jsonl"))

    def trace(self, event, data):
        self.events.append((event, data))
        self.trace_file(event, data)

    def config(self, mode="sdk", **settings):
        return SessionConfiguration(mcp_servers={"demo": {
            "command": sys.executable, "args": [str(FIXTURE.resolve()),
                "--state-dir", str(self.root / "server"), "--mode", mode], **settings}})

    def session(self, responses, config=None):
        session = Session(ScriptedClient(responses), build_file_tools(self.root),
            workspace=self.root, configuration=config or self.config(),
            trace=self.trace)
        self.addAsyncCleanup(session.close)
        return session

    async def drive(self, session):
        await session.post_user_message("Use the configured tools.")
        await asyncio.wait_for(session.run_until_idle(), 15)

    def effects(self):
        path = self.root / "server/effects.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def test_real_discovery_call_website_and_next_step_receipt(self):
        messages = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                messages.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                body = json.dumps({"accepted": True, "message_id": len(messages)}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        website = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=website.serve_forever, daemon=True)
        thread.start()
        try:
            config = self.config(env_vars=["MCP_TEST_ENDPOINT"], required=True)
            session = self.session([[call("mcp__demo__send_message", "send", text="你好，网站")],
                                    [wait()]], config)
            with patch.dict(os.environ, {"MCP_TEST_ENDPOINT":
                    f"http://127.0.0.1:{website.server_port}/messages"}):
                await self.drive(session)
            self.assertEqual(messages, [{"text": "你好，网站"}])
            requests = [data["prompt"] for event, data in self.events if event == "model_request"]
            self.assertNotIn("mcp__demo__send_message", [tool.get("name", tool["type"]) for tool in requests[0]["tools"]])
            self.assertIn("tool_search", [tool.get("name", tool["type"]) for tool in requests[0]["tools"]])
            output = json.loads(requests[1]["input"][-1]["output"])
            self.assertFalse(output["isError"])
            self.assertEqual(output["structuredContent"], {"accepted": True, "message_id": 1})
            self.assertIs(StepContext.capture(session).mcp, session.services.mcp.binding)
            await session.close()
            pid = int((self.root / "server/pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            await asyncio.to_thread(website.shutdown)
            website.server_close()

    async def test_filters_and_step_schema_snapshot(self):
        session = self.session([[wait()]], self.config(enabled_tools=["send_message", "fail"],
                                                       disabled_tools=["fail"]))
        await self.drive(session)
        context = StepContext.capture(session)
        specs = [tool for tool in context.prompt.tools if tool.get("name", "").startswith("mcp__")]
        self.assertEqual(specs, [])
        self.assertEqual([tool.name for tool in context.mcp.tools], ["send_message"])
        spec = context.mcp.tools[0].spec
        spec["parameters"]["properties"].clear()
        self.assertIn("text", context.mcp.tools[0].spec["parameters"]["properties"])

    async def test_error_timeout_and_crash_are_receipts_without_replay(self):
        for name in ("fail", "slow", "crash"):
            with self.subTest(name=name):
                session = self.session([[call(f"mcp__demo__{name}", name)], [wait(name + "_wait")]],
                                       self.config(tool_timeout_sec=0.15))
                await self.drive(session)
                result = next(item for item in session.state.history.snapshot()
                    if item.get("type") == "function_call_output" and item["call_id"] == name)
                body = json.loads(result["output"])
                self.assertTrue(body["isError"])
                if name != "fail":
                    self.assertTrue(body["error"]["outcome_unknown"])
                    self.assertEqual(sum(effect["kind"] == name for effect in self.effects()), 1)
                self.assertEqual(session.state.status, "waiting")
                await session.close()

    async def test_environment_only_forwards_named_values_and_never_live_event_channel(self):
        config = self.config(env_vars=["MCP_TEST_FORWARD", "HARNESS_GENERATOR_SOCKET"])
        session = self.session([[call("mcp__demo__environment", "environment")], [wait()]], config)
        with patch.dict(os.environ, {"MCP_TEST_FORWARD": "fixture-forwarded",
                "MCP_TEST_HIDDEN": "fixture-hidden", "HARNESS_GENERATOR_SOCKET": "fixture-channel"}):
            await self.drive(session)
        output = next(item["output"] for item in session.state.history.snapshot()
                      if item.get("call_id") == "environment" and "output" in item)
        self.assertEqual(json.loads(output)["structuredContent"],
                         {"forwarded": "fixture-forwarded", "hidden": None, "emission": None})

    async def test_pagination_repeated_cursor_and_invalid_schema(self):
        session = self.session([[wait()]], self.config("pages"))
        await self.drive(session)
        self.assertEqual([call.name for call in session.services.mcp.binding.tools], ["first", "second"])
        await session.close()
        for mode in ("repeat_cursor", "invalid_schema"):
            session = self.session([[wait(mode)]], self.config(mode))
            await self.drive(session)
            self.assertEqual(session.services.mcp.binding.tools, ())
            self.assertIn("mcp_startup_failed", session.state.history.snapshot()[1]["text"])
            await session.close()

    async def test_startup_timeout_optional_and_required_failure_cleanup(self):
        session = self.session([[wait()]], self.config("hang", startup_timeout_sec=0.1))
        await self.drive(session)
        self.assertEqual(len(session.services.mcp.failures), 1)
        await session.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "server/pid").read_text()), 0)
        required = self.session([], self.config("hang", startup_timeout_sec=0.1, required=True))
        await required.post_user_message("hi")
        with self.assertRaisesRegex(RuntimeError, "Required MCP"):
            await asyncio.wait_for(required.run_until_idle(), 10)
        self.assertEqual(required.state.status, "failed")

    async def test_resume_reconnects_same_thread_without_replaying_tools(self):
        await self.check_resume_reconnects("persistent")

    async def test_on_demand_resume_preserves_thread_without_replaying_tools(self):
        await self.check_resume_reconnects("on_demand")

    async def check_resume_reconnects(self, connection_mode):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([
            [call("mcp__demo__send_message", "send1", text="first")], [wait("wait1")]]),
            self.root, configuration=self.config(env_vars=["MCP_TEST_FORWARD"],
                connection_mode=connection_mode), trace=self.trace)
        self.addAsyncCleanup(first.close)
        await self.drive(first)
        pid = int((self.root / "server/pid").read_text())
        await first.suspend()
        second = manager.resume(ScriptedClient([
            [call("mcp__demo__send_message", "send2", text="second")], [wait("wait2")]]),
            first.thread_id, trace=self.trace)
        self.addAsyncCleanup(second.close)
        await second.run_until_idle()
        self.assertEqual(second.state.step_number, first.state.step_number)
        self.assertNotEqual(int((self.root / "server/pid").read_text()), pid)
        await self.drive(second)
        self.assertEqual([row["text"] for row in self.effects() if row["kind"] == "sent"], ["first", "second"])
        self.assertEqual(first.configuration, second.configuration)
        await second.close()

    async def test_disabled_server_does_not_launch(self):
        session = self.session([[wait()]], self.config(enabled=False))
        await self.drive(session)
        self.assertEqual(self.effects(), [])

    async def wait_for_effect(self, kind):
        for _ in range(200):
            if any(row["kind"] == kind for row in self.effects()):
                return
            await asyncio.sleep(0.02)
        self.fail(f"Fixture did not perform {kind}")

    async def test_cancelled_step_reaps_mcp_process_without_inventing_a_receipt(self):
        session = self.session([[call("mcp__demo__slow", "slow_cancel")]])
        await session.post_user_message("start")
        worker = asyncio.create_task(session.run_until_idle())
        try:
            await self.wait_for_effect("slow")
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(worker, 10)
        self.assertEqual(session.state.status, "failed")
        self.assertFalse(any(item.get("type") == "function_call_output"
                             for item in session.state.history.snapshot()))
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.root / "server/pid").read_text()), 0)

    async def test_closing_connection_resolves_in_flight_call_as_unknown(self):
        session = self.session([])
        await session._prepare_mcp_tools()
        prepared = next(tool for tool in session.services.mcp.binding.tools if tool.name == "slow")
        request = asyncio.create_task(prepared.call({}))
        try:
            await self.wait_for_effect("slow")
            await asyncio.wait_for(session.services.mcp.close(), 10)
            with self.assertRaises(McpCallError) as error:
                await asyncio.wait_for(request, 1)
            self.assertTrue(error.exception.outcome_unknown)
        finally:
            await asyncio.gather(request, return_exceptions=True)

    async def test_missing_executable_is_an_optional_startup_event(self):
        session = self.session([[wait()]], SessionConfiguration(mcp_servers={"missing": {
            "command": str(self.root / "no-such-executable")}}))
        await self.drive(session)
        self.assertEqual(len(session.services.mcp.failures), 1)
        self.assertIn("mcp_startup_failed", session.state.history.snapshot()[1]["text"])
        self.assertEqual(session.state.status, "waiting")
