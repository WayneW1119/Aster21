import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_harness.core.client import ResponsesClient, ScriptedClient
from agent_harness.core.context_manager.agent_context import AgentContext
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.registry import Tool, ToolRegistry
from agent_harness.core.tools.result import ToolResult
from agent_harness.core.tools.router import ToolRouter


def spec(name, description="original"):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": {}}}


def call(name, call_id="one", **arguments):
    return {"type": "function_call", "name": name, "call_id": call_id,
            "arguments": json.dumps(arguments)}


class StepContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, responses=(), registry=None, **kwargs):
        session = Session(ScriptedClient(list(responses)), registry or ToolRegistry(),
                          workspace=self.root, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    async def test_context_detaches_history_memory_instructions_and_settings(self):
        agent = AgentContext(self.root / "agent")
        agent.root.mkdir()
        agent.initialize("Original identity")
        agent.write_memory("index.md", "Original memory")
        session = self.session(agent_context=agent)
        session.record_items([{"type": "message", "role": "user", "text": "original"}])
        original = StepContext.capture(session)
        # Simulate an explicit user configuration change, not an agent instruction edit.
        agent.instructions_path.write_text("New identity", encoding="utf-8")
        agent.write_memory("index.md", "New memory")
        session.configuration = SessionConfiguration(base_instructions="New base")
        session.record_items([{"type": "message", "role": "user", "text": "later"}])
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            StepContext.capture(session)
        agent.write_memory("index.md", "Original memory")
        current = StepContext.capture(session)
        self.assertIn("Original identity", original.prompt.base_instructions)
        self.assertNotIn("New identity", original.prompt.base_instructions)
        self.assertIn("Original memory", original.prompt.memory["text"])
        self.assertEqual(len(original.prompt.input), 1)
        self.assertIn("New identity", current.prompt.base_instructions)
        self.assertEqual(current.prompt.memory, original.prompt.memory)
        self.assertEqual(agent.read_memory("index.md"), "Original memory")
        self.assertEqual(len(current.prompt.input), 2)
        self.assertIsNot(original.settings, current.settings)
        changed = original.prompt
        changed.tools[0]["name"] = "corrupted"
        changed.input[0]["text"] = "corrupted"
        changed.base_instructions = "corrupted"
        self.assertNotEqual(original.prompt.input[0]["text"], "corrupted")
        self.assertNotEqual(original.prompt.tools[0]["name"], "corrupted")
        self.assertNotEqual(original.prompt.base_instructions, "corrupted")

    async def test_snapshot_detaches_nested_schemas_and_handler_bindings(self):
        registry = ToolRegistry()
        tool = Tool(spec("versioned"), lambda: "old")
        registry.register(tool)
        tool.spec["parameters"]["properties"]["external"] = {"type": "string"}
        session = self.session(registry=registry)
        context = StepContext.capture(session)
        live = session.router.registry._tools["versioned"]
        live.handler = lambda: "new"
        live.spec["description"] = "new description"
        live.spec["parameters"]["properties"]["later"] = {"type": "string"}
        session.router.registry.register(Tool(spec("late"), lambda: "late"))
        captured = next(t for t in context.prompt.tools if t["name"] == "versioned")
        self.assertEqual(captured["description"], "original")
        self.assertEqual(captured["parameters"]["properties"], {})
        self.assertEqual((await context.tool_router.invoke_async(call("versioned"))).output, "old")
        self.assertIn("error", (await context.tool_router.invoke_async(call("late"))).output)
        self.assertEqual((await session.router.invoke_async(call("versioned"))).output, "new")
        with self.assertRaisesRegex(ValueError, "snapshot"):
            context.tool_router.registry.register(Tool(spec("other"), lambda: "other"))
        exposed = context.tool_router.registry.specs()
        exposed[0]["description"] = "changed"
        self.assertEqual(context.tool_router.registry.specs()[0]["description"], "original")

    async def test_updates_during_request_only_affect_next_step(self):
        entered, release = threading.Event(), threading.Event()
        registry = ToolRegistry()
        registry.register(Tool(spec("versioned"), lambda: "old implementation"))
        session = self.session([
            [call("versioned", "v1"), call("late", "l1")],
            [call("versioned", "v2"), call("late", "l2")],
            [call("send_user_message", "done", text="done", wake_model=False)],
        ], registry=registry)
        requests = []
        respond = session.client.respond

        def blocked(prompt):
            requests.append(prompt)
            if len(requests) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test request timed out")
            return respond(prompt)

        session.client.respond = blocked
        await session.post_user_message("start")
        worker = asyncio.create_task(session.run_until_idle())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            updated = session.router.registry.copy()
            updated._tools["versioned"].handler = lambda: "new implementation"
            updated._tools["versioned"].spec["description"] = "new description"
            updated.register(Tool(spec("late"), lambda: "late implementation"))
            session.router = ToolRouter(updated)
            session.configuration = SessionConfiguration(base_instructions="New base")
            await session.post_user_message("interrupt")
        finally:
            release.set()
            await asyncio.wait_for(worker, 4)
        outputs = {i["call_id"]: i["output"] for i in session.state.history.snapshot()
                   if i["type"] == "function_call_output"}
        self.assertEqual(outputs["v1"], "old implementation")
        self.assertIn("error", outputs["l1"])
        self.assertEqual(outputs["v2"], "new implementation")
        self.assertEqual(outputs["l2"], "late implementation")
        self.assertNotIn("late", [t.get("name", t["type"]) for t in requests[0].tools])
        self.assertIn("late", [t.get("name", t["type"]) for t in requests[1].tools])
        self.assertTrue(requests[1].base_instructions.startswith("New base"))
        self.assertEqual(session.state.step_number, 3)
        self.assertEqual(session.state.sent_messages, ["done"])

    async def test_transport_settings_are_captured_without_logging_credentials(self):
        session = self.session()
        original = ResponsesClient("https://example.invalid/v1/responses", "old", "fake")
        session.client = original
        context = StepContext.capture(session)
        original.model = "new"
        original.endpoint = "https://other.invalid/v1/responses"
        original.api_key = "new-fake"
        self.assertEqual(context.client.model, "old")
        self.assertEqual(context.client.endpoint, "https://example.invalid/v1/responses")
        self.assertEqual(context.client.api_key, "fake")
        self.assertNotIn("api_key", context.prompt.__dict__)
        self.assertEqual(StepContext.capture(session).client.model, "new")

    async def test_custom_client_without_capture_hook_is_supported(self):
        class Client:
            def respond(self, prompt):
                return None
        session = self.session()
        session.client = Client()
        self.assertIs(StepContext.capture(session).client, session.client)

    async def test_transport_capture_keeps_explicit_http_policy(self):
        session = self.session()
        session.client = ResponsesClient("http://example.invalid/v1/responses", "model",
                                         "fake", allow_http=True)
        self.assertEqual(StepContext.capture(session).client.endpoint,
                         "http://example.invalid/v1/responses")
        session.client = ResponsesClient("https://example.invalid/v1/responses", "model", "fake")
        session.client.endpoint = "http://example.invalid/v1/responses"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            StepContext.capture(session)

    async def test_resume_preserves_receipt_without_recreating_wake_event(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([[call("send_user_message", "send1", text="first", wake_model=False)]]),
                               self.root)
        try:
            await first.post_user_message("hello")
            await first.run_until_idle()
            await first.suspend()
            resumed = manager.resume(ScriptedClient([
                [call("send_user_message", "send2", text="second", wake_model=False)]]), first.thread_id)
            self.addAsyncCleanup(resumed.close)
            self.assertTrue(resumed.input_queue.empty())
            await resumed.run_until_idle()
            self.assertEqual(resumed.state.step_number, 1)
            await resumed.post_user_message("continue")
            await resumed.run_until_idle()
            self.assertEqual(resumed.state.step_number, 2)
            receipts = [i for i in resumed.state.history.snapshot()
                        if i["type"] == "function_call_output"]
            self.assertEqual([i["call_id"] for i in receipts], ["send1", "send2"])
            self.assertTrue(all(set(i) == {"type", "call_id", "output"} for i in receipts))
            self.assertTrue(resumed.input_queue.empty())
        finally:
            await first.close()


class ToolResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.router = ToolRouter(self.registry)

    async def test_string_and_structured_results_share_one_protocol(self):
        self.registry.register(Tool(spec("plain"), lambda: "text"))
        self.registry.register(Tool(spec("quiet"), lambda: ToolResult("receipt", wake_model=False)))
        plain = await self.router.invoke_async(call("plain"))
        quiet = await self.router.invoke_async(call("quiet"))
        self.assertTrue(plain.wake_model)
        self.assertFalse(quiet.wake_model)
        self.assertEqual(quiet.to_item("one"), {"type": "function_call_output",
                                              "call_id": "one", "output": "receipt"})
        self.assertEqual(self.router.dispatch(call("quiet")), quiet.to_item("one"))
        self.assertEqual(await self.router.dispatch_async(call("quiet")), quiet.to_item("one"))

    async def test_async_handler_can_return_structured_result(self):
        async def quiet():
            return ToolResult("async receipt", wake_model=False)
        self.registry.register(Tool(spec("quiet"), quiet))
        self.assertFalse((await self.router.invoke_async(call("quiet"))).wake_model)
        # Sync invocation of an async tool is a waking error, not an unawaited coroutine.
        result = self.router.invoke(call("quiet"))
        self.assertTrue(result.wake_model)
        self.assertIn("error", result.output)

    async def test_errors_and_invalid_results_always_wake(self):
        def failed():
            raise OSError("delivery failed")
        self.registry.register(Tool(spec("failed"), failed, run_inline=True))
        self.registry.register(Tool(spec("invalid"), lambda: {"wake_model": False}))
        bad_json = call("failed")
        bad_json["arguments"] = "not json"
        array_args = call("failed")
        array_args["arguments"] = "[]"
        for item in (call("failed"), call("invalid"), call("missing"), bad_json, array_args):
            for result in (self.router.invoke(item), await self.router.invoke_async(item)):
                self.assertTrue(result.wake_model)
                self.assertIn("error", result.output)

    async def test_inline_callback_stays_on_event_loop_and_blocking_tool_does_not(self):
        loop_thread = threading.get_ident()
        self.registry.register(Tool(spec("inline"), lambda: str(threading.get_ident()), run_inline=True))
        self.registry.register(Tool(spec("thread"), lambda: str(threading.get_ident())))
        self.assertEqual((await self.router.invoke_async(call("inline"))).output, str(loop_thread))
        self.assertNotEqual((await self.router.invoke_async(call("thread"))).output, str(loop_thread))

    async def test_invalid_result_metadata_rejected(self):
        for output, wake in (({}, False), ("text", "false"), ("text", 0)):
            with self.assertRaises(TypeError):
                ToolResult(output, wake_model=wake)

    async def test_non_terminal_tool_can_be_non_waking_and_record_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            self.registry.register(Tool(spec("quiet"), lambda: ToolResult("stored", wake_model=False)))
            session = Session(ScriptedClient([[call("quiet")]]), self.registry,
                              workspace=Path(directory))
            try:
                await session.post_user_message("start")
                await session.run_until_idle()
                self.assertEqual(session.state.step_number, 1)
                self.assertTrue(session.input_queue.empty())
                self.assertEqual(session.state.history.snapshot()[-1]["output"], "stored")
                self.assertEqual(session.state.sent_messages, [])
            finally:
                await session.close()
