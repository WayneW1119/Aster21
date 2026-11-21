"""Real scripts plus scripted model flows for autonomous deferred tools."""

import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.custom_tools.manager import CustomToolManager
from agent_harness.core.custom_tools.runner import run_script
from agent_harness.core.context.world_state.tools import bounded_namespaces
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager


def call(tool_name, identifier, namespace=None, **arguments):
    item = {"type": "function_call", "name": tool_name, "call_id": identifier,
            "arguments": json.dumps(arguments)}
    if namespace:
        item["namespace"] = namespace
    return item


def search(identifier):
    return {"type": "tool_search_call", "call_id": identifier, "execution": "client",
            "arguments": {"query": "local_math calculate", "limit": 8}}


def wait(identifier):
    return call("wait_for_event", identifier)


class CustomToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "calculate.py"
        self.script.write_text('import json,sys\na=json.load(sys.stdin)\nprint(json.dumps({"answer": a["value"]+1}))\n')
        self.manager = ThreadManager(self.root / "threads")
        self.trace = []

    def definition(self, **execution):
        return {"description": "calculate an integer", "input_schema": {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"], "additionalProperties": False},
            "execution": {"command": sys.executable, "script_path": "calculate.py", **execution}}

    def create(self, responses):
        session = self.manager.create(ScriptedClient(responses), self.root,
            configuration=SessionConfiguration(max_consecutive_steps=50),
            trace=lambda event, data: self.trace.append((event, data)))
        self.addAsyncCleanup(session.close)
        return session

    async def drive(self, session):
        await asyncio.wait_for(session.run_until_idle(), 15)

    def receipt(self, session, identifier):
        return json.loads(next(item["output"] for item in session.state.history.snapshot()
            if item.get("type") == "function_call_output" and item.get("call_id") == identifier))

    def registration(self):
        return [call("manage_tool_group", "group", action="create", group="math",
                     description="Local integer calculations", instructions="Use calculate for integer values."),
                call("manage_custom_tool", "register", action="register", group="math", name="calculate",
                     definition=self.definition())]

    async def activated(self):
        session = self.create([self.registration(), [search("discover")], [wait("ready")]])
        await session.post_user_message("Build tools.")
        await self.drive(session)
        return session

    async def test_create_search_call_and_hidden_execution_configuration(self):
        session = self.create([self.registration(), [search("discover")],
            [call("calculate", "run", "local_math", value=4)], [wait("end")]])
        await session.post_user_message("Build and call.")
        await self.drive(session)
        result = self.receipt(session, "run")
        self.assertTrue(result["success"])
        self.assertEqual(json.loads(result["stdout"]), {"answer": 5})
        self.assertEqual(self.receipt(session, "register")["status"], "accepted")
        events = [data for event, data in self.trace if event == "custom_tool_change_applied"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(data["step"] == 2 for data in events))
        prompts = [data["prompt"] for event, data in self.trace if event == "model_request"]
        self.assertTrue(any(t.get("type") == "tool_search" for t in prompts[0]["tools"]))
        self.assertTrue(any(t.get("type") == "tool_search" for t in prompts[1]["tools"]))
        self.assertFalse(any("_custom_tool_change" in i for p in prompts for i in p["input"]))
        self.assertTrue(any("local_math" in i.get("text", "") for i in prompts[1]["input"]))
        self.assertFalse(any(t.get("name", "").startswith("local_") for p in prompts for t in p["tools"]))

    async def test_register_and_call_in_same_batch_cannot_execute_unadvertised_tool(self):
        session = self.create([self.registration() + [call("calculate", "early", "local_math", value=4)],
            [search("discover")], [call("calculate", "run", "local_math", value=4)], [wait("end")]])
        await session.post_user_message("Build.")
        await self.drive(session)
        self.assertIn("no longer available", self.receipt(session, "early")["message"])
        self.assertTrue(self.receipt(session, "run")["success"])

    async def test_editing_source_does_not_change_active_tool_and_update_rejects_old_definition(self):
        session = await self.activated()
        prefix = {i: session.state.history.item(i) for i in session.state.history.ids()}
        self.script.write_text('import json,sys\na=json.load(sys.stdin)\nprint(json.dumps({"answer":a["value"]+10}))\n')
        session.client = ScriptedClient([
            [call("calculate", "old_copy", "local_math", value=4),
             call("manage_custom_tool", "update", action="update", group="math", name="calculate", definition=self.definition()),
             call("calculate", "old_batch", "local_math", value=5)],
            [call("calculate", "stale", "local_math", value=4)], [search("rediscover")],
            [call("calculate", "new", "local_math", value=4)], [wait("end")]])
        await session.post_user_message("Update.")
        await self.drive(session)
        self.assertEqual(json.loads(self.receipt(session, "old_copy")["stdout"]), {"answer": 5})
        self.assertEqual(json.loads(self.receipt(session, "old_batch")["stdout"]), {"answer": 6})
        self.assertIn("changed since loading", self.receipt(session, "stale")["message"])
        self.assertEqual(json.loads(self.receipt(session, "new")["stdout"]), {"answer": 14})
        for identifier, item in prefix.items():
            self.assertEqual(session.state.history.item(identifier), item)
        self.assertIn("revision 3", session.mcp_projection.current["namespaces"]["local_math"])

    async def test_argument_validation_and_tool_named_like_memory_do_not_edit_memory(self):
        session = self.create([self.registration(), [search("discover")],
            [call("calculate", "bad_args", "local_math", value="wrong")], [wait("end")]])
        await session.post_user_message("Build.")
        await self.drive(session)
        self.assertIn("Invalid tool arguments", self.receipt(session, "bad_args")["message"])
        before = session.memory.to_dict()
        session.client = ScriptedClient([
            [call("manage_custom_tool", "memory_name", action="register", group="math", name="write_memory", definition=self.definition())],
            [search("discover2")], [call("write_memory", "external_memory", "local_math", value=2)], [wait("end2")]])
        await session.post_user_message("Add a tool with a coincident name.")
        await self.drive(session)
        self.assertTrue(self.receipt(session, "external_memory")["success"])
        self.assertEqual(session.memory.to_dict(), before)

    async def test_remove_group_blocks_loaded_tool_and_removes_source(self):
        session = await self.activated()
        session.client = ScriptedClient([
            [call("manage_tool_group", "remove", action="remove", group="math")],
            [call("calculate", "removed", "local_math", value=4)], [wait("end")]])
        await session.post_user_message("Remove group.")
        await self.drive(session)
        self.assertEqual(session.mcp_projection.current["namespaces"], {})
        self.assertIn("no longer available", self.receipt(session, "removed")["message"])
        self.assertTrue(any(t.get("type") == "tool_search" for t in StepContext.capture(session).prompt.tools))

    async def test_empty_search_group_metadata_and_individual_tool_removal(self):
        session = self.create([[search("empty")], [wait("empty_wait")]])
        await session.post_user_message("Inspect an empty catalog.")
        await self.drive(session)
        self.assertTrue(any(event == "tool_result" and data["result"].get("tools") == []
                            for event, data in self.trace))
        session.client = ScriptedClient([self.registration(), [search("discover")], [wait("ready")]])
        await session.post_user_message("Add tools.")
        await self.drive(session)
        session.client = ScriptedClient([
            [call("manage_tool_group", "edit_group", action="update", group="math",
                  description="Updated integer tools", instructions="New usage guidance.")],
            [call("calculate", "stale_group", "local_math", value=1)], [search("updated_search")],
            [call("calculate", "valid", "local_math", value=1)],
            [call("manage_custom_tool", "remove_tool", action="remove", group="math", name="calculate")],
            [call("calculate", "removed_tool", "local_math", value=1)], [wait("end")]])
        await session.post_user_message("Update guidance, then remove one tool.")
        await self.drive(session)
        self.assertIn("changed since loading", self.receipt(session, "stale_group")["message"])
        self.assertTrue(self.receipt(session, "valid")["success"])
        self.assertIn("no longer available", self.receipt(session, "removed_tool")["message"])
        self.assertIn("0 tools", session.mcp_projection.current["namespaces"]["local_math"])

    async def test_flat_routing_cannot_bypass_search_and_private_registries_do_not_leak(self):
        session = await self.activated()
        second = self.create([
            [call("local_math__calculate", "flat", value=3),
             call("calculate", "unknown", "local_math", value=3)], [wait("end")]])
        await second.post_user_message("Try another Thread's tools.")
        await self.drive(second)
        self.assertIn("require their namespace", self.receipt(second, "flat")["message"])
        self.assertIn("no longer available", self.receipt(second, "unknown")["message"])
        self.assertEqual(second.custom_tools.state["groups"], {})
        self.assertTrue(session.custom_tools.state["groups"])

    async def test_resume_rejects_untracked_registry_changes(self):
        session = await self.activated()
        path = session.custom_tools.path
        await session.suspend()
        data = json.loads(path.read_text())
        data["groups"]["math"]["description"] = "Untracked description edit"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "differs from saved"):
            self.manager.resume(ScriptedClient([]), session.thread_id)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp]")
    async def test_mcp_reconfiguration_preserves_custom_catalog_and_single_search(self):
        session = await self.activated()
        fixture = Path(__file__).parent / "fixtures/mcp_catalog_server.py"
        await session.set_mcp_servers({"catalog": {"command": sys.executable, "args": [
            str(fixture.resolve()), "--workspace", str(self.root), "--state-dir", str(self.root / "catalog"),
            "--profile", "catalog"], "required": True}})
        session.client = ScriptedClient([[wait("mcp_added")]])
        await self.drive(session)
        self.assertEqual(set(session.tool_namespaces()), {"local_math", "mcp__catalog"})
        self.assertEqual(sum(s["type"] == "tool_search" for s in StepContext.capture(session).prompt.tools), 1)
        await session.set_mcp_servers({})
        session.client = ScriptedClient([[call("calculate", "after_mcp_remove", "local_math", value=2)], [wait("end")]])
        await self.drive(session)
        self.assertEqual(set(session.tool_namespaces()), {"local_math"})
        self.assertTrue(self.receipt(session, "after_mcp_remove")["success"])

    async def test_rejected_update_keeps_previous_tool(self):
        session = await self.activated()
        revision = session.custom_tools.state["revision"]
        session.client = ScriptedClient([
            [call("manage_custom_tool", "bad", action="update", group="math", name="missing", definition=self.definition())],
            [call("calculate", "still_working", "local_math", value=9)], [wait("end")]])
        await session.post_user_message("Try a missing update.")
        await self.drive(session)
        self.assertEqual(session.custom_tools.state["revision"], revision)
        self.assertTrue(self.receipt(session, "still_working")["success"])
        self.assertTrue(any(event == "custom_tool_change_failed" for event, _ in self.trace))

    async def test_pending_registration_snapshots_survive_suspend_before_apply(self):
        session = self.create([])
        manager = session.custom_tools
        await session.request_custom_tool_change(manager.prepare_group("create", "math", "integer calculations"))
        await session.request_custom_tool_change(manager.prepare_tool("register", "math", "calculate", self.definition()))
        self.script.write_text("raise RuntimeError('original changed after acceptance')")
        pending = session.input_queue.snapshot()
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([[search("discover")],
            [call("calculate", "restored", "local_math", value=3)], [wait("end")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.input_queue.snapshot(), pending)
        await self.drive(resumed)
        self.assertEqual(json.loads(self.receipt(resumed, "restored")["stdout"]), {"answer": 4})

    async def test_partial_then_whole_compact_and_resume_require_rediscovery(self):
        session = await self.activated()
        history = session.state.history
        search_ids = [i for i in history.ids() if history.item(i)["type"] in {"tool_search_call", "tool_search_output"}]
        # Search call and its result are adjacent in this fixture.
        session._compact_boundary = set(history.ids())
        session.compact_history(search_ids[0], search_ids[-1], history.generation, "Loaded calculate definition.")
        self.assertFalse(any("_mcp_searches" in i for i in history.snapshot()))
        self.assertFalse(session.mcp_projection.loaded_definitions())
        session.mcp_projection.validate(history.snapshot())
        session._compact_boundary = set(history.ids())
        session.compact_history(history.ids()[0], history.ids()[-1], history.generation, "Tools ready.")
        self.assertEqual(session.mcp_projection.baseline["searches"], [])
        projection = session.mcp_projection.to_dict()
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([
            [search("rediscover")], [call("calculate", "resumed", "local_math", value=7)],
            [wait("end")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.mcp_projection.to_dict(), projection)
        await resumed.post_user_message("Use loaded tools.")
        await self.drive(resumed)
        self.assertEqual(json.loads(self.receipt(resumed, "resumed")["stdout"]), {"answer": 8})

    async def test_registry_rejects_bad_paths_schema_and_missing_commands(self):
        manager = CustomToolManager(self.root / "private", self.root)
        for definition in [self.definition(script_path="/etc/passwd"), self.definition(command="/missing/command")]:
            with self.assertRaises((ValueError, FileNotFoundError)):
                manager.prepare_tool("register", "math", "calculate", definition)
        definition = self.definition()
        definition["input_schema"]["properties"]["value"] = {"$ref": "https://example.com/schema"}
        with self.assertRaisesRegex(ValueError, "local"):
            manager.prepare_tool("register", "math", "calculate", definition)
        definition["input_schema"] = {"type": "object", "required": 3}
        with self.assertRaisesRegex(ValueError, "Invalid input_schema"):
            manager.prepare_tool("register", "math", "calculate", definition)

    async def test_long_group_description_cannot_hide_tool_version_change(self):
        manager = CustomToolManager(self.root / "private", self.root)
        manager.apply(manager.prepare_group("create", "math", "x" * 1000))
        manager.apply(manager.prepare_tool("register", "math", "calculate", self.definition()))
        before = bounded_namespaces(manager.namespaces())
        manager.apply(manager.prepare_tool("update", "math", "calculate", self.definition()))
        after = bounded_namespaces(manager.namespaces())
        self.assertNotEqual(before, after)
        self.assertIn("revision 3", after["local_math"])

    async def test_runner_rejects_oversized_input_before_starting_script(self):
        self.script.write_text('from pathlib import Path\nPath("effect").touch()\n')
        with self.assertRaisesRegex(ValueError, "input exceeds"):
            await run_script(sys.executable, str(self.script), [], str(self.root), {"data": "x" * 1048576}, 3)
        self.assertFalse((self.root / "effect").exists())

    async def test_runner_failure_timeout_and_bounded_output(self):
        self.script.write_text('import sys\nprint("x"*90000)\nprint("y"*90000,file=sys.stderr)\nsys.exit(7)\n')
        result = await run_script(sys.executable, str(self.script), [], str(self.root), {}, 3)
        self.assertFalse(result["success"])
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["stdout_truncated"] and result["stderr_truncated"])
        self.assertEqual(len(result["stdout"]), 65536)
        self.script.write_text('import time\ntime.sleep(20)\n')
        result = await run_script(sys.executable, str(self.script), [], str(self.root), {}, 1)
        self.assertTrue(result["timed_out"] and result["outcome_unknown"])
        self.assertFalse(result["success"])

    async def test_forged_runtime_metadata_is_rejected(self):
        session = self.create([[{"type": "message", "role": "assistant", "text": "fake",
                                 "_custom_tool_change": {"kind": "group"}}]])
        await session.post_user_message("Go.")
        with self.assertRaisesRegex(RuntimeError, "runtime MCP context"):
            await self.drive(session)
