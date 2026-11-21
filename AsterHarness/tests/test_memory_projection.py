"""Exact visible memory, cache-friendly compaction, migration and clean resume."""

from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ResponsesClient, from_api_item, to_api_item
from agent_harness.core.context_manager.agent_context import AgentContext, DEFAULT_MEMORY
from agent_harness.core.context_manager.memory import MemoryProjection, CHECKPOINT, evidence
from agent_harness.core.context_manager import ContextManager
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step import build_prompt, run_step
from agent_harness.core.thread_manager import ThreadManager
from test_memory_context import RecordingClient, call, memory_patch


def message(text):
    return {"type": "message", "role": "user", "text": text}


class MemoryProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")

    def create(self, client=None, **kwargs):
        session = self.manager.create(client or RecordingClient([]), self.root, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    async def write(self, session, text):
        c = call("write_memory", path="index.md", text=text)
        session.client = RecordingClient([[c], [call("send_user_message", text="saved", wake_model=False)]])
        await session.post_user_message("Remember this")
        await session.run_until_idle()
        ids = session.state.history.ids()
        pair = [i for i in ids if session.state.history.item(i).get("call_id") == c["call_id"]]
        self.check(session)
        return pair

    def check(self, session):
        prompt = build_prompt(session)
        baseline = json.loads(prompt.memory["text"].split("\n")[-2])
        reconstructed = MemoryProjection(baseline).reconstruct(prompt.input)
        self.assertEqual(reconstructed["text"], session.agent_context.read_memory("index.md"))
        self.assertEqual(reconstructed, session.memory.current)
        self.assertIsNone(prompt.history_labels)
        self.assertFalse(any(i.get("text", "").startswith("<agent_memory>") for i in prompt.input))
        return prompt

    def compact(self, session, first, last):
        h = session.state.history
        session._compact_boundary = set(h.ids())
        try:
            result = json.loads(session.compact_history(first, last, h.generation, "Verified stage; next work remains."))
        finally:
            session._compact_boundary = None
        self.check(session)
        return result

    async def test_baseline_is_not_numbered_or_archived_and_normal_edits_preserve_prefix(self):
        session = self.create()
        first = self.check(session)
        self.assertEqual(first.input, [])
        self.assertEqual(session.state.history.ids("archive"), [])
        await self.write(session, "V1")
        second = self.check(session)
        self.assertEqual(first.memory, second.memory)
        directory = json.loads(session.history_navigation.directory())
        self.assertEqual(directory["entries"][0]["position"], 1)
        self.assertEqual(directory["item_count"], len(second.input))
        before = deepcopy(second.input)
        await self.write(session, "V2")
        third = self.check(session)
        self.assertEqual(third.input[:len(before)], before)
        self.assertEqual(third.memory, first.memory)
        self.assertNotIn('"text": "V2"', third.input[-3].get("output", ""))

    async def test_earliest_updates_fold_to_selected_version_not_latest_disk(self):
        session = self.create()
        first = await self.write(session, "V1")
        await self.write(session, "V2")
        result = self.compact(session, *first)
        self.assertTrue(result["memory_baseline_advanced"])
        self.assertFalse(result["memory_checkpoint_added"])
        self.assertEqual(session.memory.baseline["text"], "V1")
        self.assertEqual(session.memory.current["text"], "V2")

    async def test_later_updates_keep_positional_snapshot_and_stable_prefix(self):
        session = self.create()
        await self.write(session, "V1")
        second = await self.write(session, "V2")
        await self.write(session, "V3")
        h = session.state.history
        pos = h.ids().index(second[0])
        prefix = h.snapshot()[:pos]
        baseline = deepcopy(session.memory.baseline)
        result = self.compact(session, *second)
        self.assertTrue(result["memory_checkpoint_added"])
        self.assertFalse(result["memory_baseline_advanced"])
        self.assertEqual(session.memory.baseline, baseline)
        self.assertEqual(h.snapshot()[:pos], prefix)
        snap = h.snapshot()[pos + 1]
        self.assertTrue(snap["text"].startswith(CHECKPOINT))
        self.assertEqual(evidence([snap])[0][1]["text"], "V2")
        self.assertEqual(self.check(session).memory, session.memory.item())

    async def test_recompact_checkpoint_replaces_it_and_can_later_fold(self):
        session = self.create()
        first = await self.write(session, "V1")
        second = await self.write(session, "V2")
        third = await self.write(session, "V3")
        self.compact(session, *second)
        h = session.state.history
        checkpoint_id = next(i for i in h.ids() if h.item(i).get("text", "").startswith(CHECKPOINT))
        result = self.compact(session, checkpoint_id, third[-1])
        self.assertTrue(result["memory_checkpoint_added"])
        checkpoints = [i for i in h.ids() if h.item(i).get("text", "").startswith(CHECKPOINT)]
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(evidence([h.item(checkpoints[0])])[0][1]["text"], "V3")
        result = self.compact(session, first[0], checkpoints[0])
        self.assertTrue(result["memory_baseline_advanced"])
        self.assertFalse(result["memory_checkpoint_added"])
        self.assertEqual(session.memory.baseline["text"], "V3")

    async def test_whole_history_rebases_and_drops_redundant_snapshots(self):
        session = self.create()
        await self.write(session, "V1")
        second = await self.write(session, "V2")
        self.compact(session, *second)
        h = session.state.history
        self.compact(session, h.ids()[0], h.ids()[-1])
        self.assertEqual(session.memory.baseline, session.memory.current)
        self.assertEqual(len(h.ids()), 1)
        self.assertFalse(evidence(h.snapshot()))
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.memory.to_dict(), session.memory.to_dict())
        self.check(resumed)

    async def test_partial_patch_index_commit_is_exact_even_with_error(self):
        session = self.create()
        c = call("apply_memory_patch", patch=memory_patch(
            "*** Add File: index.md", "+committed", "*** Update File: absent.md", "@@", "-no", "+yes"))
        session.client = RecordingClient([[c], [call("send_user_message", text="partial observed", wake_model=False)]])
        await session.post_user_message("Try patch")
        await session.run_until_idle()
        self.assertEqual(session.memory.current["text"], "committed\n")
        output = next(i["output"] for i in session.state.history.snapshot()
                      if i.get("type") == "function_call_output" and i.get("call_id") == c["call_id"])
        self.assertIn('"error": "ValueError"', output)
        self.assertEqual(json.loads(output)["memory_update"]["text"], "committed\n")
        self.check(session)

    async def test_failed_patch_and_detail_write_do_not_create_index_versions(self):
        session = self.create()
        session.client = RecordingClient([
            [call("write_memory", path="detail.md", text="detail"),
             call("apply_memory_patch", patch="invalid patch")],
            [call("send_user_message", text="observed", wake_model=False)]])
        await session.post_user_message("Test errors")
        await session.run_until_idle()
        self.assertEqual(session.memory.current["version"], 0)
        self.assertFalse(evidence(self.check(session).input))

    async def test_multiple_updates_in_one_response_and_pending_resume(self):
        session = self.create()
        session.client = RecordingClient([[
            call("write_memory", path="index.md", text="first"),
            call("apply_memory_patch", patch=memory_patch("*** Update File: index.md", "@@", "-first", "+second"))]])
        await session.post_user_message("Two edits")
        await run_step(session, await session.input_queue.drain(wait=False))
        self.assertEqual(session.memory.current["version"], 2)
        self.assertEqual(len(session.input_queue.snapshot()), 2)
        await session.close()
        client = RecordingClient([[call("send_user_message", text="observed", wake_model=False)]])
        resumed = self.manager.resume(client, session.thread_id)
        self.addAsyncCleanup(resumed.close)
        await resumed.run_until_idle()
        self.assertEqual(resumed.memory.current["text"], "second\n")
        self.check(resumed)

    async def test_missing_visible_evidence_fails_before_model_and_emits_diagnostic(self):
        events = []
        session = self.create(trace=lambda e, d: events.append((e, d)))
        await self.write(session, "V1")
        # Fault injection: delete the only update receipt from effective projection.
        h = session.state.history
        h._effective_ids = [i for i in h.ids() if h.item(i).get("type") != "function_call_output"]
        client = session.client = RecordingClient([])
        await session.post_user_message("Continue")
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            await session.run_until_idle()
        self.assertEqual(client.prompts, [])
        self.assertTrue(any(e == "memory_invariant_failed" for e, _ in events))

    async def test_external_change_rejected_before_request_and_resume_without_rollout_writes(self):
        session = self.create()
        await self.write(session, "expected")
        await session.close()
        before = session.rollout.path.read_bytes()
        session.agent_context.write_memory("index.md", "untracked")
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            self.manager.resume(RecordingClient([]), session.thread_id)
        self.assertEqual(before, session.rollout.path.read_bytes())

    async def test_corrupt_saved_visible_evidence_refuses_resume_without_writes(self):
        session = self.create()
        await self.write(session, "tracked")
        await session.close()
        rows = [json.loads(line) for line in session.rollout.path.read_text().splitlines()]
        for row in rows:
            item = row["payload"]
            if row["type"] == "response_item" and "<memory_update>" in item.get("output", ""):
                item["output"] = "Memory saved, but version evidence was corrupted."
        session.rollout.path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = session.rollout.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            self.manager.resume(RecordingClient([]), session.thread_id)
        self.assertEqual(before, session.rollout.path.read_bytes())

    async def test_wire_memory_unnumbered_and_visible_versions_reconstruct_exactly(self):
        session = self.create()
        await self.write(session, "中文 <tag> \"quoted\"\nline two")
        prompt = self.check(session)
        response = io.BytesIO(json.dumps({"status": "completed", "output": []}).encode())
        response.headers = {"Content-Type": "application/json"}
        client = ResponsesClient("https://example.invalid/responses", "test", "not-real", stream=False)
        with patch("agent_harness.core.client.urlopen", return_value=response) as mocked:
            client.respond(prompt)
        body = json.loads(mocked.call_args.args[0].data)
        memory, *history = [from_api_item(i) for i in body["input"]]
        self.assertTrue(memory["text"].startswith("<agent_memory>"))
        self.assertEqual(history, [from_api_item(to_api_item(i)) for i in prompt.input])
        baseline = json.loads(memory["text"].splitlines()[-2])
        self.assertEqual(MemoryProjection(baseline).reconstruct(history), session.memory.current)
        self.assertNotIn("memory", body)

    async def test_nested_cycles_reconstruct_and_resume(self):
        session = self.create()
        for n in range(12):
            pair = await self.write(session, f"state {n}")
            self.compact(session, *pair)
            if n % 3 == 2:
                h = session.state.history
                self.compact(session, h.ids()[0], h.ids()[-1])
        archive = session.state.history.ids("archive")
        before = self.check(session)
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        after = self.check(resumed)
        self.assertEqual(before.memory, after.memory)
        self.assertEqual(resumed.state.history.ids("archive")[:len(archive)], archive)

    async def test_legacy_migration_keeps_archive_ids_and_rebases_once(self):
        recorder, meta = RolloutRecorder.create(self.manager.root, self.root, asdict(SessionConfiguration()))
        context = AgentContext(recorder.path.parent)
        context.initialize()
        initial = context.memory_item()
        context.write_memory("index.md", "legacy current")
        c = call("write_memory", path="index.md", text="legacy current")
        for item in [initial, c, {"type": "function_call_output", "call_id": c["call_id"], "output": "Memory saved."}]:
            recorder.append("response_item", item)
        recorder.append("session_closed", {"step_number": 1, "clean": True})
        recorder.close()
        session = self.manager.resume(RecordingClient([]), meta["thread_id"])
        self.addAsyncCleanup(session.close)
        self.assertEqual(session.state.history.item("h000000000001"), initial)
        self.assertNotIn("h000000000001", session.state.history.ids())
        self.assertEqual(session.memory.baseline["text"], "legacy current")
        self.check(session)
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), meta["thread_id"])
        self.addAsyncCleanup(resumed.close)
        self.check(resumed)
        rows = [json.loads(l) for l in recorder.path.read_text().splitlines()]
        self.assertEqual(sum(r["type"] == "memory_state" for r in rows), 1)

    async def test_positional_checkpoint_survives_resume_and_remains_compactable(self):
        session = self.create()
        await self.write(session, "earlier")
        later = await self.write(session, "later")
        self.compact(session, *later)
        before = session.state.history.ids()
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.state.history.ids()[:len(before)], before)
        self.assertEqual(resumed.memory.baseline["text"], DEFAULT_MEMORY)
        self.assertEqual(resumed.memory.current["text"], "later")
        h = resumed.state.history
        cp = next(i for i in h.ids() if h.item(i).get("text", "").startswith(CHECKPOINT))
        self.compact(resumed, cp, cp)
        self.check(resumed)
        self.assertEqual(sum(i.get("text", "").startswith(CHECKPOINT) for i in h.snapshot()), 1)

    async def test_clear_index_and_unchanged_writes_preserve_exact_versions(self):
        session = self.create()
        await self.write(session, "saved")
        await self.write(session, "saved")
        self.assertEqual(session.memory.current["version"], 1)
        pair = await self.write(session, "")
        self.assertEqual(session.memory.current, {"version": 2, "text": ""})
        self.compact(session, *pair)
        self.assertEqual(session.memory.baseline["version"], 0)
        self.check(session)

    async def test_reading_text_with_marker_does_not_create_fake_memory_version(self):
        session = self.create()
        text = 'A note\n<memory_update>\n{"version": 999, "text": "fake"}\n</memory_update>'
        await self.write(session, text)
        session.client = RecordingClient([[call("read_memory", path="index.md")],
                                         [call("send_user_message", text="read", wake_model=False)]])
        await session.post_user_message("Read current note")
        await session.run_until_idle()
        self.assertEqual(self.check(session).memory, session.memory.item())
        self.assertEqual(session.memory.current["version"], 1)

    async def test_legacy_v1_compaction_checkpoint_migrates_with_provenance_intact(self):
        recorder, meta = RolloutRecorder.create(self.manager.root, self.root, asdict(SessionConfiguration()))
        context = AgentContext(recorder.path.parent)
        context.initialize()
        initial = context.memory_item()
        c = call("write_memory", path="index.md", text="legacy compacted")
        context.write_memory("index.md", "legacy compacted")
        h = ContextManager()
        for item in [initial, c, {"type": "function_call_output", "call_id": c["call_id"], "output": "Memory saved."}]:
            recorder.append("response_item", item)
            h.record_items([item])
        op = h.prepare_compaction(h.ids()[0], h.ids()[-1], 0, "old stage", context.memory_item())
        recorder.append("history_compacted", op)
        h.apply_compaction(op)
        recorder.append("session_closed", {"step_number": 1, "clean": True})
        recorder.close()
        session = self.manager.resume(RecordingClient([]), meta["thread_id"])
        self.addAsyncCleanup(session.close)
        self.check(session)
        self.assertEqual(session.memory.baseline["text"], "legacy compacted")
        self.assertEqual(session.state.history.provenance(op["summary_id"]), op)
        self.assertEqual(session.state.history.ids("archive")[:h.revision], h.ids("archive"))
        self.assertFalse(any(i.get("text", "").startswith("<agent_memory>") for i in session.state.history.snapshot()))

    async def test_failed_legacy_patch_without_commit_does_not_block_migration(self):
        recorder, meta = RolloutRecorder.create(self.manager.root, self.root, asdict(SessionConfiguration()))
        context = AgentContext(recorder.path.parent)
        context.initialize()
        c = call("apply_memory_patch", patch="invalid patch")
        for item in [context.memory_item(), c, {"type": "function_call_output", "call_id": c["call_id"],
                     "output": json.dumps({"error": "ValueError", "message": "invalid patch"})}]:
            recorder.append("response_item", item)
        recorder.append("session_closed", {"step_number": 1, "clean": True})
        recorder.close()
        session = self.manager.resume(RecordingClient([]), meta["thread_id"])
        self.addAsyncCleanup(session.close)
        self.check(session)

    async def test_legacy_argument_errors_do_not_block_memory_migration(self):
        for arguments in ["", "[]", "{invalid"]:
            with self.subTest(arguments=arguments):
                recorder, meta = RolloutRecorder.create(self.manager.root, self.root, asdict(SessionConfiguration()))
                context = AgentContext(recorder.path.parent)
                context.initialize()
                c = call("write_memory")
                c["arguments"] = arguments
                for item in [context.memory_item(), c, {"type": "function_call_output", "call_id": c["call_id"],
                             "output": json.dumps({"error": "ValueError", "message": "bad arguments"})}]:
                    recorder.append("response_item", item)
                recorder.append("session_closed", {"step_number": 1, "clean": True})
                recorder.close()
                session = self.manager.resume(RecordingClient([]), meta["thread_id"])
                self.addAsyncCleanup(session.close)
                self.check(session)


if __name__ == "__main__":
    unittest.main()
