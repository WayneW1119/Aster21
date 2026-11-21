"""Range safety, provenance, cursor generations and durable compact/resume."""

import json
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.context_manager import ContextManager
from agent_harness.core.context.world_state.memory import MemoryState
from agent_harness.core.session import Session
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.history import HistoryReader
from test_memory_context import RecordingClient, call


def message(text):
    return {"type": "message", "role": "user", "text": text}


def receipt(tool_call, output="done"):
    return {"type": "function_call_output", "call_id": tool_call["call_id"], "output": output}


class CompactHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = ContextManager()
        self.history.record_items([message("a" * 4000), message("b" * 4000), message("keep")])

    def compact(self, first=0, last=1, summary="Earlier work verified a and b."):
        ids = self.history.ids()
        op = self.history.prepare_compaction(ids[first], ids[last], self.history.generation, summary)
        self.history.apply_compaction(op)
        return op

    def test_originals_preserved_ids_stable_projection_shrinks(self):
        original = self.history.snapshot()
        op = self.compact()
        self.assertEqual(self.history.ids(), [op["summary_id"], "h000000000003"])
        self.assertEqual(self.history.generation, 1)
        self.assertEqual(self.history.revision, 4)
        self.assertEqual([self.history.item(i) for i in self.history.ids("archive")[:3]], original)
        self.assertLess(len(json.dumps(self.history.snapshot())), len(json.dumps(original)))
        info = self.history.provenance(op["summary_id"])
        info["source_ids"].clear()
        self.assertEqual(self.history.provenance(op["summary_id"])["replaced_count"], 2)

    def test_nested_provenance_counts_leaves_without_expanding(self):
        first = self.compact()
        second = self.compact(summary="Combined stage handoff")
        info = self.history.provenance(second["summary_id"])
        self.assertEqual(info["source_ids"], [first["summary_id"], "h000000000003"])
        self.assertEqual(info["replaced_count"], 2)
        self.assertEqual(info["leaf_count"], 3)

    def test_stale_generation_bad_range_and_empty_summary_leave_history_unchanged(self):
        ids = self.history.ids()
        for args in ((ids[0], ids[1], 1, "x"), (ids[1], ids[0], 0, "x"),
                     ("missing", ids[0], 0, "x"), (ids[0], ids[1], 0, " "),
                     (ids[0], ids[1], True, "x"), (ids[0], ids[1], 0, "x" * 16001)):
            with self.subTest(args=args[:3]), self.assertRaises(ValueError):
                self.history.prepare_compaction(*args)
        self.assertEqual(self.history.ids(), ids)
        self.assertEqual(self.history.revision, 3)

    def test_call_pairs_cannot_be_split_and_pending_calls_block(self):
        c = call("exec_command", cmd="slow")
        self.history.record_items([c])
        ids = self.history.ids()
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.history.prepare_compaction(ids[-1], ids[-1], 0, "running")
        self.history.record_items([receipt(c, '{"session_id": 123, "status":"running"}')])
        ids = self.history.ids()
        for start, end in ((ids[-1], ids[-1]), (ids[0], ids[-2])):
            with self.assertRaises(ValueError):
                self.history.prepare_compaction(start, end, 0, "split")
        # A returned running handle is a complete protocol pair, even before exit.
        op = self.history.prepare_compaction(ids[-2], ids[-1], 0, "Command handle 123 still running")
        self.history.apply_compaction(op)

    def test_effective_cursor_expires_archive_cursor_survives_replacement(self):
        reader = HistoryReader(self.history, "scope")
        pages = {view: json.loads(reader.query(view, None, None, 1, 1024))
                 for view in ("archive", "effective")}
        self.compact()
        with self.assertRaisesRegex(ValueError, "cursor"):
            reader.query("effective", pages["effective"]["next_cursor"], None, 1, 1024)
        archive = json.loads(reader.query("archive", pages["archive"]["next_cursor"], None, 1, 1024))
        self.assertEqual(archive["snapshot_revision"], 3)
        effective = json.loads(reader.query("effective", None, None, 1, 24000))
        self.assertEqual(effective["effective_generation"], 1)
        self.assertEqual(effective["entries"][0]["id"], "h000000000004")

    def test_effective_pagination_after_replacement_uses_archive_ids_not_positions(self):
        self.compact()
        reader = HistoryReader(self.history, "scope")
        first = json.loads(reader.query("effective", None, None, 1, 24000))
        self.history.record_items([message("new arrival")])
        second = json.loads(reader.query("effective", first["next_cursor"], None, 1, 24000))
        self.assertEqual(first["entries"][0]["id"], "h000000000004")
        self.assertEqual(second["entries"][0]["id"], "h000000000003")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(second["snapshot_revision"], 4)

    def test_corrupt_operations_are_rejected_without_partial_mutation(self):
        ids = self.history.ids()
        op = self.history.prepare_compaction(ids[0], ids[1], 0, "summary")
        for change in ({"summary_id": "h999"}, {"leaf_count": 30},
                       {"source_ids": [ids[0], ids[2]]}, {"version": 2}, {"version": True},
                       {"memory_checkpoint": message("fake")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.history.apply_compaction(op | change)
            self.assertEqual(self.history.ids(), ids)
            self.assertEqual(self.history.revision, 3)


class CompactSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")

    def create(self, client=None):
        session = self.manager.create(client or RecordingClient([]), self.root)
        self.addAsyncCleanup(session.close)
        return session

    def prepare_call(self, session, start=None, end=None):
        ids = session.state.history.ids()
        return call("compact_history", start_id=start or ids[0], end_id=end or ids[-1],
                    expected_generation=session.state.history.generation,
                    summary="Task maintained; stage complete, no publication. Next verify remaining work.")

    async def test_real_step_compact_receipt_next_request_then_resume_projection(self):
        client = RecordingClient([])
        session = self.create(client)
        session.record_items([message("old details " * 1000)])
        originals = session.state.history.ids()
        tool = self.prepare_call(session)
        # Use a fresh scripted client, keeping the session and registry bindings intact.
        session.client = client = RecordingClient([[tool], [call("send_user_message", text="done", wake_model=False)]])
        await session.post_user_message("Compact earlier work, keep no-publication constraint.")
        await session.run_until_idle()
        history = session.state.history
        summary_id = next(i for i in history.ids() if "<history_summary" in history.item(i).get("text", ""))
        self.assertIn("old details", json.dumps(client.prompts[0].input))
        self.assertNotIn("old details", json.dumps(client.prompts[1].input))
        self.assertTrue(any(i.get("call_id") == tool["call_id"] and i["type"] == "function_call_output"
                            for i in client.prompts[1].input))
        self.assertTrue(set(originals) <= set(history.ids("archive")))
        self.assertTrue(all("history_id" not in item for p in client.prompts for item in p.input))
        expected = history.ids()
        archive = history.ids("archive")
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.state.history.ids()[:len(expected)], expected)
        self.assertEqual(resumed.state.history.ids("archive")[:len(archive)], archive)
        self.assertEqual(resumed.state.history.generation, 1)
        self.assertEqual(resumed.state.history.provenance(summary_id)["source_ids"], originals)
        self.assertNotIn("old details", json.dumps(build_prompt(resumed).input))

    async def test_multiple_tool_batch_rejected_and_current_response_protected(self):
        session = self.create()
        session.record_items([message("old work")])
        tool = self.prepare_call(session)
        session.client = RecordingClient([[tool, call("send_user_message", text="batch", wake_model=False)], []])
        await session.post_user_message("test")
        await session.run_until_idle()
        self.assertEqual(session.state.history.generation, 0)
        self.assertIn("only tool call", json.dumps(session.state.history.snapshot()))
        session._compact_boundary = set(session.state.history.ids())
        c = call("send_user_message", text="new", wake_model=False)
        session.record_items([c, receipt(c)])
        ids = session.state.history.ids()
        with self.assertRaisesRegex(ValueError, "current response"):
            session.compact_history(ids[-2], ids[-1], 0, "new batch")
        session._compact_boundary = None

    async def test_pending_compact_receipt_survives_resume_without_reexecuting(self):
        session = self.create()
        session.record_items([message("archive this")])
        tool = self.prepare_call(session)
        session.client = RecordingClient([[tool]])
        old_trace = session.trace

        def pause_after_commit(event, data):
            old_trace(event, data)
            if event == "history_compacted":
                session._suspending = True

        session.trace = pause_after_commit
        await session.post_user_message("compact then suspend")
        await session.run_until_idle()
        self.assertEqual(len(session.input_queue.snapshot()), 1)
        await session.close()
        client = RecordingClient([[call("send_user_message", text="receipt observed", wake_model=False)]])
        resumed = self.manager.resume(client, session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(len(resumed.input_queue.snapshot()), 1)
        await resumed.run_until_idle()
        self.assertEqual(resumed.state.history.generation, 1)
        self.assertEqual(resumed.state.sent_messages, ["receipt observed"])
        self.assertTrue(any(i.get("call_id") == tool["call_id"] and i["type"] == "function_call_output"
                            for i in client.prompts[0].input))

    async def test_live_background_process_does_not_block_compacting_returned_handle(self):
        session = self.create()
        c = call("exec_command", cmd="sleep 2", yield_time_ms=1, max_output_tokens=100,
                 notify_on_completion=True)
        session.client = RecordingClient([[c], []])
        await session.post_user_message("start background fixture")
        await session.run_until_idle()
        ids = session.state.history.ids()
        pair = [i for i in ids if session.state.history.item(i).get("call_id") == c["call_id"]]
        self.assertEqual(len(pair), 2)
        self.assertIn("session_id", session.state.history.item(pair[1])["output"])
        self.assertTrue(session.generator_manager.has_active)
        session._compact_boundary = set(ids)
        result = json.loads(session.compact_history(pair[0], pair[1], 0, "Background job running; handle remains usable"))
        self.assertEqual(result["replaced_count"], 2)
        self.assertTrue(session.generator_manager.has_active)

    async def test_event_arriving_during_compact_request_is_preserved(self):
        session = self.create()
        session.record_items([message("old details")])
        compact_call = self.prepare_call(session)
        entered = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()
        client = RecordingClient([[compact_call], [call("send_user_message", text="new input observed", wake_model=False)]])
        original = client.respond

        def respond(prompt):
            if not client.prompts:
                loop.call_soon_threadsafe(entered.set)
                asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=5)
            return original(prompt)

        client.respond = respond
        session.client = client
        await session.post_user_message("compact earlier")
        worker = asyncio.create_task(session.run_until_idle())
        await asyncio.wait_for(entered.wait(), timeout=5)
        await session.post_user_message("NEW-USER-CORRECTION: still do not publish")
        release.set()
        await asyncio.wait_for(worker, timeout=5)
        self.assertEqual(session.state.history.generation, 1)
        self.assertIn("NEW-USER-CORRECTION", json.dumps(client.prompts[1].input))

    async def test_memory_edits_checkpoint_supersedes_old_anchor_and_is_durable(self):
        session = self.create()
        c = call("write_memory", path="index.md", text="Current verified status")
        session.client = RecordingClient([[c], [call("send_user_message", text="saved", wake_model=False)]])
        await session.post_user_message("remember")
        await session.run_until_idle()
        ids = session.state.history.ids()
        # Only remove the memory write pair, leaving the older initial anchor.
        start = next(i for i in ids if session.state.history.item(i).get("call_id") == c["call_id"])
        end = next(i for i in ids if session.state.history.item(i)["type"] == "function_call_output"
                   and session.state.history.item(i).get("call_id") == c["call_id"])
        session.client = RecordingClient([[self.prepare_call(session, start, end)], []])
        await session.post_user_message("compact")
        await session.run_until_idle()
        anchors = [i for i in session.state.history.snapshot() if MemoryState.matches_retained_fragment(i)]
        self.assertEqual(len(anchors), 0)
        self.assertIn("Current verified status", build_prompt(session).memory["text"])
        await session.close()
        # Resume must use the saved checkpoint, not silently reload external changes.
        session.agent_context.write_memory("index.md", "external changed")
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            self.manager.resume(RecordingClient([]), session.thread_id)
        session.agent_context.write_memory("index.md", "Current verified status")
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertNotIn("external changed", json.dumps(build_prompt(resumed).input))
        self.assertEqual(len([i for i in resumed.state.history.snapshot()
                              if MemoryState.matches_retained_fragment(i)]), 0)
        self.assertIn("Current verified status", build_prompt(resumed).memory["text"])

    async def test_provenance_tool_paginates_and_nested_sources_are_queryable(self):
        session = self.create()
        session.record_items([message(str(i)) for i in range(80)])
        session._compact_boundary = set(session.state.history.ids())
        ids = session.state.history.ids()
        first = json.loads(session.compact_history(ids[0], ids[-1], 0, "80 originals"))
        args = {"summary_id": first["summary_id"], "offset": 0, "limit": 50}
        page = json.loads(session.router.registry.execute("query_compaction", args))
        self.assertEqual(len(page["source_ids"]), 50)
        next_page = json.loads(session.router.registry.execute("query_compaction", args | {"offset": page["next_offset"]}))
        self.assertEqual(len(next_page["source_ids"]), 30)
        self.assertIsNone(next_page["next_offset"])
        session._compact_boundary = set(session.state.history.ids())
        ids = session.state.history.ids()
        second = json.loads(session.compact_history(ids[0], ids[-1], 1, "All work"))
        self.assertEqual(second["leaf_count"], 80)
        self.assertIn(first["summary_id"], session.state.history.provenance(second["summary_id"])["source_ids"])

    async def test_persistence_error_fails_session_instead_of_successful_tool_receipt(self):
        session = self.create()
        session.record_items([message("details")])
        session.client = RecordingClient([[self.prepare_call(session)]])
        append = session.rollout.append

        def failing(kind, *args, **kwargs):
            if kind == "history_compacted":
                session.rollout.failed = True
                raise OSError("disk full")
            return append(kind, *args, **kwargs)

        with patch.object(session.rollout, "append", side_effect=failing):
            await session.post_user_message("compact")
            with self.assertRaisesRegex(RuntimeError, "persist compaction"):
                await session.run_until_idle()
        self.assertEqual(session.state.status, "failed")
        self.assertEqual(session.state.history.generation, 0)

    async def test_corrupt_compaction_rollout_rejected_without_writes(self):
        session = self.create()
        session.record_items([message("ordinary work")])
        session._compact_boundary = set(session.state.history.ids())
        ids = session.state.history.ids()
        session.compact_history(ids[0], ids[0], 0, "Initial empty memory")
        await session.close()
        path = session.rollout.path
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        next(r for r in rows if r["type"] == "history_compacted")["payload"]["leaf_count"] = 999
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "compaction"):
            self.manager.resume(RecordingClient([]), session.thread_id)
        self.assertEqual(path.read_bytes(), before)

    async def test_nonpersistent_session_supports_compaction_without_memory(self):
        session = Session(RecordingClient([]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(session.close)
        session.record_items([message("one"), message("two")])
        session._compact_boundary = set(session.state.history.ids())
        ids = session.state.history.ids()
        result = json.loads(session.compact_history(ids[0], ids[1], 0, "Combined"))
        self.assertFalse(result["memory_checkpoint_added"])
        self.assertEqual(len(session.state.history.snapshot()), 1)
