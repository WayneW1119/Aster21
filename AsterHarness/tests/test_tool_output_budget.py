import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.output_budget import ToolOutputStore, size
from agent_harness.core.tools.registry import Tool, ToolRegistry
from agent_harness.core.session import Session


def call(name, identifier, **arguments):
    return {"type": "function_call", "name": name, "call_id": identifier, "arguments": json.dumps(arguments)}


class OutputBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ToolOutputStore(self.root / "outputs")

    async def test_unicode_json_preview_and_paging_are_lossless_and_bounded(self):
        original = json.dumps({"data": '世界\\"\n' * 8000}, ensure_ascii=False)
        result = {"type": "function_call_output", "call_id": "long", "output": original}
        projected, info = self.store.limit(result, call("long", "long"), 512)
        self.assertLessEqual(size(projected["output"]), 2048)
        self.assertTrue(json.loads(projected["output"])["tool_output_budget"]["truncated"])
        pages, offset = [], 0
        while True:
            raw = self.store.read(info["output_id"], offset, max_chars=100000, token_limit=512)
            self.assertLessEqual(size(raw), 2048)
            page = json.loads(raw)
            pages.append(page["content"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(pages), original)
        self.assertEqual(result["output"], original)

    async def test_image_pixels_do_not_count_as_text_and_are_never_cut(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 90000}
        result = {"type": "function_call_output", "call_id": "image", "output": [
            {"type": "input_text", "text": "x" * 20000}, image]}
        visible, info = self.store.limit(result, call("image", "image"), 512)
        self.assertIsNotNone(info)
        self.assertEqual(visible["output"][1], image)
        self.assertLessEqual(size(visible["output"][0]["text"]), 2048)

    async def test_protected_memory_update_survives_tiny_budget_and_partial_error(self):
        memory = {"version": 2, "text": "记忆" * 2000}
        output = "large diagnostic " * 1000 + "\n<memory_update>\n" + json.dumps(memory, ensure_ascii=False) + "\n</memory_update>"
        result = {"type": "function_call_output", "call_id": "patch", "output": output}
        visible, info = self.store.limit(result, call("apply_memory_patch", "patch"), 256, memory_tool=True)
        self.assertTrue(info["protected_memory_evidence_exceeds_budget"])
        self.assertTrue(visible["output"].endswith(output[output.index("\n<memory_update>\n"):]))
        result["output"] = json.dumps({"error": "PatchError", "message": "x" * 20000, "memory_update": memory}, ensure_ascii=False)
        visible, _ = self.store.limit(result, call("apply_memory_patch", "patch"), 256, memory_tool=True)
        self.assertEqual(json.loads(visible["output"])["memory_update"], memory)

    async def test_search_trims_whole_definitions_and_keeps_loading_state_consistent(self):
        registry = ToolRegistry()
        members = [Tool({"type": "function", "name": "small" + str(i), "description": "probe arithmetic",
            "parameters": {"type": "object", "properties": {}}}, lambda: "ok", deferred=True) for i in range(8)]
        members.append(Tool({"type": "function", "name": "huge", "description": "probe " + "x" * 20000,
            "parameters": {"type": "object", "properties": {}}}, lambda: "must not execute", deferred=True))
        registry.register_namespace("native_probe", "Arithmetic probes", members)
        search = {"type": "tool_search_call", "call_id": "search", "execution": "client",
                  "arguments": {"query": "probe", "limit": 20}}
        session = Session(ScriptedClient([[search], [call("wait_for_event", "wait")]]), registry,
            workspace=self.root, configuration=SessionConfiguration(tool_output_token_limit=256))
        self.addAsyncCleanup(session.close)
        await session.post_user_message("Find probes.")
        await session.run_until_idle()
        loaded = session.mcp_projection.loaded_definitions()
        self.assertTrue(loaded)
        self.assertLess(len(loaded), 9)
        self.assertNotIn(("native_probe", "huge"), loaded)
        session.mcp_projection.validate(session.state.history.snapshot())
        output = next(i for i in session.state.history.snapshot() if i["type"] == "tool_search_output")
        self.assertLessEqual(size(json.dumps(output["tools"], ensure_ascii=False)), 1024)

    async def test_runtime_logs_full_result_and_artifact_survives_compact_resume(self):
        manager = ThreadManager(self.root / "threads")
        events = []
        session = manager.create(ScriptedClient([[call("long", "long")], [call("wait_for_event", "wait")]]),
            self.root, configuration=SessionConfiguration(tool_output_token_limit=512),
            trace=lambda event, data: events.append((event, data)))
        self.addAsyncCleanup(session.close)
        original = "BEGIN" + "长内容" * 10000 + "END"
        session.router.registry.register(Tool({"type": "function", "name": "long", "description": "Long output",
            "parameters": {"type": "object", "properties": {}}}, lambda: original))
        await session.post_user_message("Read.")
        await session.run_until_idle()
        raw = next(data["result"] for event, data in events if event == "tool_result_raw")
        self.assertEqual(raw["output"], original)
        info = next(data for event, data in events if event == "tool_output_limited")
        history = session.state.history
        session._compact_boundary = set(history.ids())
        session.compact_history(history.ids()[0], history.ids()[-1], history.generation, "Saved output " + info["output_id"])
        await session.suspend()
        resumed = manager.resume(ScriptedClient([[call("read_tool_output", "page", output_id=info["output_id"], offset=0)],
            [call("wait_for_event", "end")]]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        await resumed.post_user_message("Read saved output.")
        await resumed.run_until_idle()
        page = next(i for i in resumed.state.history.snapshot() if i.get("call_id") == "page" and i["type"] == "function_call_output")
        self.assertTrue(original.startswith(json.loads(page["output"])["content"]))
        self.assertEqual(resumed.configuration.tool_output_token_limit, 512)

    async def test_memory_invariant_holds_after_budgeted_patch_and_request(self):
        original, updated = "旧" * 3000, "新" * 3000
        patch = "*** Begin Patch\n*** Update File: index.md\n@@\n-" + original + "\n+" + updated + "\n*** End Patch"
        session = ThreadManager(self.root / "threads").create(ScriptedClient([
            [call("write_memory", "write", path="index.md", text=original)],
            [call("apply_memory_patch", "patch", patch=patch)], [call("wait_for_event", "end")]]), self.root,
            configuration=SessionConfiguration(tool_output_token_limit=256))
        self.addAsyncCleanup(session.close)
        await session.post_user_message("Edit memory.")
        await session.run_until_idle()
        prompt = StepContext.capture(session).prompt
        self.assertEqual(session.memory.reconstruct(prompt.input)["text"], updated + "\n")

    async def test_disabled_budget_and_configuration_validation(self):
        result = {"type": "function_call_output", "output": "x" * 100000, "call_id": "a"}
        self.assertEqual(self.store.limit(result, call("a", "a"), None), (result, None))
        for value in (True, 0, -1, 255, 1000001, "1000"):
            with self.assertRaises(ValueError):
                SessionConfiguration(tool_output_token_limit=value)
        for identifier in ("../escape", "out_..", "out_" + "x" * 32):
            with self.assertRaises(ValueError):
                self.store.read(identifier)
