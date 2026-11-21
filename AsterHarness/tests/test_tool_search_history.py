"""Tool discovery follows effective history; old retention formats migrate safely."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agent_harness.core.client import ScriptedClient
from agent_harness.core.context_manager.mcp import McpProjection, request_items
from agent_harness.core.context.world_state.tools import sources_item
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager


def pair(identifier, name):
    return [{"type": "tool_search_call", "call_id": identifier, "execution": "client",
             "arguments": {"query": name}},
            {"type": "tool_search_output", "call_id": identifier, "execution": "client",
             "status": "completed", "tools": [{"type": "namespace", "name": "demo",
                 "tools": [{"type": "function", "name": name, "description": name,
                            "parameters": {"type": "object", "properties": {}}}]}]}]


class SearchHistoryTests(unittest.IsolatedAsyncioTestCase):
    def test_partial_compaction_removes_only_covered_definitions_and_keeps_sources(self):
        first, second = pair("a", "first"), pair("b", "second")
        source = sources_item({"demo": "Test tools"})
        items = [source] + first + second
        projection = McpProjection()
        projection.observe(items)
        original = projection.to_dict()
        state, checkpoints = projection.plan_compaction(items, 0, 2)
        self.assertEqual(projection.to_dict(), original)  # Planning is not committing.
        self.assertEqual(len(checkpoints), 1)
        self.assertIn("_mcp_sources", checkpoints[0])
        self.assertFalse(any("_mcp_searches" in item for item in checkpoints))
        current = McpProjection.from_dict(state)
        current.validate(checkpoints + second)
        self.assertEqual(set(current.loaded_definitions()), {("demo", "second")})
        self.assertEqual(current.current["namespaces"], {"demo": "Test tools"})
        self.assertFalse(any(i["type"].startswith("tool_search") for i in current.prefix_items()))

    def test_many_repeated_searches_leave_no_definition_collection_after_whole_compact(self):
        items = [item for i in range(40) for item in pair(f"repeat-{i}", "same_tool")]
        projection = McpProjection({"namespaces": {"demo": "Tools"}, "searches": []})
        projection.observe(items)
        self.assertEqual(len(projection.current["searches"]), 40)
        state, checkpoints = projection.plan_compaction(items, 0, len(items) - 1, whole=True)
        restored = McpProjection.from_dict(state)
        restored.validate([])
        self.assertEqual(checkpoints, [])
        self.assertEqual(restored.loaded_definitions(), {})
        self.assertEqual(state["baseline"]["searches"], [])
        self.assertEqual(state["current"]["searches"], [])
        self.assertNotIn("same_tool", json.dumps(restored.prefix_items()))
        self.assertIn("demo", json.dumps(restored.prefix_items()))

    def test_legacy_baseline_and_checkpoint_are_retired_but_real_visible_search_survives(self):
        pinned, retained, visible = pair("old-base", "pinned"), pair("old-checkpoint", "retained"), pair("visible", "live")
        legacy = {"version": 1,
                  "baseline": {"namespaces": {"demo": "Tools"}, "searches": [pinned]},
                  "current": {"namespaces": {"demo": "Tools"}, "searches": [pinned, retained, visible]}}
        checkpoint = {"type": "message", "role": "developer", "text": "Old retained definitions",
                      "_mcp_searches": [retained]}
        items = [checkpoint] + visible
        original = deepcopy(items)
        projection = McpProjection.from_dict(legacy)
        self.assertEqual(projection.to_dict(), legacy)  # Old operation validation remains exact.
        self.assertTrue(projection.migrate_legacy(items))
        self.assertFalse(projection.migrate_legacy(items))
        projection.validate(items)
        self.assertEqual(set(projection.loaded_definitions()), {("demo", "live")})
        self.assertEqual(projection.to_dict()["version"], 2)
        self.assertEqual(projection.baseline["searches"], [])
        self.assertEqual(request_items(items), visible)
        self.assertEqual(items, original)
        corrupted = deepcopy(legacy)
        corrupted["current"]["namespaces"] = {"forged": "missing evidence"}
        bad = McpProjection.from_dict(corrupted)
        with self.assertRaisesRegex(RuntimeError, "legacy migration"):
            bad.migrate_legacy(items)
        self.assertEqual(bad.to_dict(), corrupted)

    def test_archive_text_and_summary_cannot_reactivate_tools_and_v2_rejects_pinned_schemas(self):
        original = pair("old", "archived")
        projection = McpProjection()
        textual = [{"type": "message", "role": "user", "text": "Previously loaded archived tool"},
                   {"type": "function_call", "name": "query_history", "call_id": "query", "arguments": "{}"},
                   {"type": "function_call_output", "call_id": "query", "output": json.dumps(original[1])}]
        projection.observe(textual)
        self.assertEqual(projection.loaded_definitions(), {})
        bad = projection.to_dict()
        bad["baseline"]["searches"] = [original]
        with self.assertRaisesRegex(ValueError, "cannot be pinned"):
            McpProjection.from_dict(bad)
        bad = projection.to_dict()
        bad["current"]["searches"] = [original]
        with self.assertRaisesRegex(RuntimeError, "invariant"):
            McpProjection.from_dict(bad).validate(textual)

    async def test_old_clean_thread_migrates_without_rewriting_archive_or_replaying_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = ThreadManager(root / "threads")
            session = manager.create(ScriptedClient([]), root)
            old = pair("old-search", "old_tool")
            session.record_items(old)
            history = session.state.history
            original_ids = history.ids()
            legacy = {"version": 1,
                      "baseline": {"namespaces": {"demo": "Tools"}, "searches": [old]},
                      "current": {"namespaces": {"demo": "Tools"}, "searches": [old]}}
            operation = history.prepare_compaction(original_ids[0], original_ids[-1], history.generation,
                "Old compact retained discovered definitions.", memory_state=session.memory.to_dict(),
                mcp_state=legacy, mcp_checkpoints=[])
            session.rollout.append("history_compacted", operation)
            history.apply_compaction(operation)
            rollout = session.rollout.path
            thread_id = session.thread_id
            await session.suspend()
            before = rollout.read_bytes()
            resumed = manager.resume(ScriptedClient([]), thread_id)
            try:
                self.assertEqual(resumed.mcp_projection.loaded_definitions(), {})
                self.assertEqual(resumed.mcp_projection.to_dict()["version"], 2)
                self.assertFalse(any(i["type"] == "tool_search_output" for i in build_prompt(resumed).input))
                self.assertEqual([resumed.state.history.item(i) for i in original_ids], old)
                self.assertTrue(rollout.read_bytes().startswith(before))
            finally:
                await resumed.suspend()
            # A second resume uses v2 directly; no repeated migration or resurrection.
            again = manager.resume(ScriptedClient([]), thread_id)
            try:
                self.assertEqual(again.mcp_projection.loaded_definitions(), {})
            finally:
                await again.suspend()
