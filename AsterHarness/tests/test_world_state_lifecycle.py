"""A third section exercises context, checkpoints and resume without Session branches."""

from copy import deepcopy
import json
import importlib.util
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient, to_api_item
from agent_harness.core.context.world_state import (
    ContextSection, SectionCompaction, WorldState, WorldStateUpdate)
from agent_harness.core.context_manager.history import ContextManager
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager


class EnvironmentSection(ContextSection):
    ID = "test_environment"
    MARKER = "<test_environment>\n"

    def __init__(self, value="initial"):
        self.baseline = {"version": 0, "value": value}
        self.current = deepcopy(self.baseline)
        self.saved = None

    def export(self):
        return {"baseline": deepcopy(self.baseline), "current": deepcopy(self.current)}

    def snapshot_state(self):
        return self.export()

    def restore(self, state):
        if set(state) != {"baseline", "current"}:
            raise ValueError("Invalid test environment")
        self.baseline, self.current = deepcopy(state["baseline"]), deepcopy(state["current"])

    def change(self, value):
        self.current = {"version": self.current["version"] + 1, "value": value}

    def item(self, value):
        return {"type": "message", "role": "developer",
                "text": self.MARKER + json.dumps(value)}

    def prefix_items(self):
        return [self.item(self.baseline)]

    def reconstruct(self, items):
        current = deepcopy(self.baseline)
        for item in items:
            if item.get("text", "").startswith(self.MARKER):
                value = json.loads(item["text"][len(self.MARKER):])
                if value["version"] > current["version"]:
                    current = value
        return current

    def validate(self, items):
        if self.reconstruct(items) != self.current:
            raise RuntimeError("Test environment invariant failed")

    def render_diff(self, previous, items):
        additions = () if self.reconstruct(items) == self.current else (self.item(self.current),)
        snapshot = deepcopy(self.current)
        return (WorldStateUpdate(self.ID, snapshot, additions)
                if previous != snapshot or additions else None)

    def observe(self, items):
        state = self.export()
        if self.reconstruct(items) == self.current and state != self.saved:
            self.saved = deepcopy(state)
            return [("section_state", {"section_id": self.ID, "state": state})]
        return []

    def plan_compaction(self, items, first, last, whole):
        state = self.export()
        selected = [item for item in items[first:last + 1]
                    if item.get("text", "").startswith(self.MARKER)]
        if whole:
            state["baseline"] = deepcopy(self.current)
            checkpoints = ()
        else:
            checkpoints = (deepcopy(selected[-1]),) if selected else ()
        return SectionCompaction(self.ID, state, checkpoints)

    def validate_compaction(self, plan, items):
        prospective = EnvironmentSection()
        prospective.restore(plan.state)
        prospective.validate(items)

    def commit_compaction(self, plan):
        self.restore(plan.state)


class WorldStateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.manager = ThreadManager(self.root / "threads")

    def session(self):
        session = self.manager.create(ScriptedClient([]), self.workspace)
        self.addAsyncCleanup(session.close)
        return session

    def compact(self, session, first, last):
        history = session.state.history
        session._compact_boundary = set(history.ids())
        return json.loads(session.compact_history(first, last, history.generation,
                                                  "Preserve current verified state."))

    async def test_prefix_is_pinned_updates_append_and_third_section_resumes(self):
        session = self.session()
        section = EnvironmentSection("A")
        session.add_world_state_section(section)
        session.record_items([{"type": "message", "role": "user", "text": "start"}])
        session.record_step_world_state_if_changed()
        first = session.build_world_state().render_context(session.state.history.snapshot())
        section.change("B")
        session.record_step_world_state_if_changed()
        second = session.build_world_state().render_context(session.state.history.snapshot())
        self.assertEqual(first.prefix, second.prefix)
        self.assertEqual(second.history[:len(first.history)], first.history)
        self.assertIn('"value": "B"', second.history[-1]["text"])
        # The Prompt.memory compatibility adapter produces the same wire sequence.
        prompt = build_prompt(session)
        wire = ([to_api_item(prompt.memory)] if prompt.memory else []) + [to_api_item(i) for i in prompt.input]
        self.assertEqual(wire, [to_api_item(i) for i in second.input_items()])
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        with self.assertRaisesRegex(RuntimeError, "test_environment"):
            build_prompt(resumed)
        restored = EnvironmentSection("wrong initialization must be replaced")
        resumed.add_world_state_section(restored)
        self.assertEqual(restored.export(), section.export())
        build_prompt(resumed)

    async def test_local_then_whole_compaction_with_memory_and_generic_section(self):
        session = self.session()
        section = EnvironmentSection("A")
        session.add_world_state_section(section)
        session.record_items([{"type": "message", "role": "user", "text": "keep before"}])
        for value in ("B", "C"):
            section.change(value)
            session.record_step_world_state_if_changed()
        history = session.state.history
        changed = history.ids()[-1]
        local = self.compact(session, changed, changed)
        self.assertFalse(local["memory_baseline_advanced"])
        self.assertEqual(section.baseline["value"], "A")
        self.assertEqual(section.reconstruct(history.snapshot()), section.current)
        self.assertEqual(history.provenance(local["summary_id"])["version"], 4)
        self.assertTrue(any('"value": "C"' in i.get("text", "") for i in history.snapshot()))
        whole = self.compact(session, history.ids()[0], history.ids()[-1])
        self.assertEqual(section.baseline, section.current)
        self.assertFalse(any(i.get("text", "").startswith(section.MARKER) for i in history.snapshot()))
        archive_before = history.snapshot()
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        restored = EnvironmentSection()
        resumed.add_world_state_section(restored)
        self.assertEqual(restored.current["value"], "C")
        self.assertEqual(resumed.state.history.snapshot()[:len(archive_before)], archive_before)
        build_prompt(resumed)
        self.assertEqual(resumed.state.history.provenance(whole["summary_id"])["version"], 4)

    async def test_section_attached_without_a_step_still_persists_its_baseline(self):
        session = self.session()
        session.add_world_state_section(EnvironmentSection("saved without a Step"))
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        restored = EnvironmentSection()
        resumed.add_world_state_section(restored)
        self.assertEqual(restored.current["value"], "saved without a Step")
        build_prompt(resumed)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "optional MCP SDK not installed")
    async def test_three_sections_keep_sources_but_rediscover_definitions_after_compact_resume(self):
        fixture = Path(__file__).parent / "fixtures/mcp_server.py"
        config = SessionConfiguration(mcp_servers={"demo": {
            "command": sys.executable, "args": [str(fixture), "--state-dir", str(self.root / "server")],
            "required": True}})
        search = {"type": "tool_search_call", "call_id": "search", "execution": "client",
                  "arguments": {"query": "mcp__demo send_message", "limit": 1}}
        wait = {"type": "function_call", "name": "wait_for_event", "arguments": "{}", "call_id": "wait"}
        write = {"type": "function_call", "name": "write_memory", "call_id": "write",
                 "arguments": json.dumps({"path": "index.md", "text": "verified work"})}
        session = self.manager.create(ScriptedClient([[write], [search], [wait]]), self.workspace,
                                      configuration=config)
        self.addAsyncCleanup(session.close)
        section = EnvironmentSection("A")
        session.add_world_state_section(section)
        await session.post_user_message("Write memory and discover the demo tool.")
        await session.run_until_idle()
        loaded = session.mcp_projection.loaded_definitions()
        self.assertIn(("mcp__demo", "send_message"), loaded)
        section.change("B")
        session.record_step_world_state_if_changed()
        history = session.state.history
        compacted = self.compact(session, history.ids()[0], history.ids()[-1])
        operation = history.provenance(compacted["summary_id"])
        self.assertEqual(set(operation["world_state_sections"]), {"memory", "tools", "test_environment"})
        self.assertTrue(compacted["memory_baseline_advanced"])
        await session.suspend()
        invoke = {"type": "function_call", "namespace": "mcp__demo", "name": "send_message",
                  "call_id": "after_resume", "arguments": json.dumps({"text": "hello from restored definitions"})}
        resumed = self.manager.resume(ScriptedClient([[{**search, "call_id": "rediscover"}],
            [invoke], [{**wait, "call_id": "wait_again"}]]),
                                      session.thread_id)
        self.addAsyncCleanup(resumed.close)
        restored = EnvironmentSection()
        resumed.add_world_state_section(restored)
        self.assertEqual(resumed.mcp_projection.loaded_definitions(), {})
        self.assertIn("mcp__demo", resumed.mcp_projection.baseline["namespaces"])
        self.assertEqual(resumed.memory.current["text"], "verified work")
        self.assertEqual(restored.current["value"], "B")
        await resumed.post_user_message("Use the discovered tool.")
        await resumed.run_until_idle()
        prompt = build_prompt(resumed)
        retained = [i for i in prompt.input if i.get("type") == "tool_search_output"]
        self.assertEqual(len(retained), 1)
        self.assertNotIn("_mcp_searches", json.dumps(prompt.input))
        self.assertIn("hello from restored definitions", (self.root / "server/effects.jsonl").read_text())

    async def test_validation_and_durable_write_fail_before_mutating_state(self):
        session = self.session()
        section = EnvironmentSection("A")
        session.add_world_state_section(section)
        section.change("B")
        session.record_step_world_state_if_changed()
        history = session.state.history
        before = history.snapshot()
        initial = section.export()
        boundary = history.ids()
        session._compact_boundary = set(boundary)
        with patch.object(section, "validate_compaction", side_effect=ValueError("invalid candidate")):
            with self.assertRaisesRegex(ValueError, "invalid candidate"):
                session.compact_history(boundary[0], boundary[-1], history.generation, "valid summary")
        self.assertEqual(history.snapshot(), before)
        with patch.object(session.rollout, "append", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(RuntimeError, "persist compaction"):
                session.compact_history(boundary[0], boundary[-1], history.generation, "valid summary")
        self.assertEqual(history.snapshot(), before)
        self.assertEqual(section.export(), initial)

    def test_generic_storage_rejects_invalid_checkpoints_and_legacy_mixing(self):
        history = ContextManager()
        history.record_items([{"type": "message", "role": "user", "text": "x"}])
        identifier = history.ids()[0]
        bad = {"test_environment": {"state": {}, "checkpoints": [{"type": "function_call"}]}}
        with self.assertRaisesRegex(ValueError, "positional checkpoint"):
            history.prepare_compaction(identifier, identifier, 0, "valid summary", world_state_sections=bad)
        good = {"test_environment": {"state": {}, "checkpoints": []}}
        with self.assertRaisesRegex(ValueError, "mix generic and legacy"):
            history.prepare_compaction(identifier, identifier, 0, "valid summary", mcp_state={},
                                       world_state_sections=good)

    def test_duplicate_and_reserved_section_ids_are_rejected(self):
        session = self.session()
        session.add_world_state_section(EnvironmentSection())
        with self.assertRaisesRegex(ValueError, "unique"):
            session.add_world_state_section(EnvironmentSection())
        section = EnvironmentSection()
        section.ID = "memory"
        with self.assertRaisesRegex(ValueError, "reserved"):
            session.add_world_state_section(section)
