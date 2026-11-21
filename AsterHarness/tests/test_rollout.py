import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.core.client import ScriptedClient
from agent_harness.core.rollout import RolloutRecorder
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.thread_manager import ThreadManager


def send(text, call_id):
    return {"type": "function_call", "name": "send_user_message", "call_id": call_id,
            "arguments": json.dumps({"text": text, "wake_model": False})}


class RolloutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("hello\n", encoding="utf-8")
        self.manager = ThreadManager(self.root / "threads")

    def create(self, responses=(), **kwargs):
        session = self.manager.create(ScriptedClient(responses), self.workspace, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    def resume(self, thread_id, responses=(), **kwargs):
        session = self.manager.resume(ScriptedClient(responses), thread_id, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    async def test_round_trip_restores_exact_items_config_workspace_and_step(self):
        reasoning = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
        text = {"type": "message", "role": "assistant", "text": "internal only"}
        read = {"type": "function_call", "name": "read_file", "call_id": "read_1",
                "arguments": '{"path":"README.md","start_line":1,"end_line":1}'}
        config = SessionConfiguration(base_instructions="Custom persistent instructions", max_consecutive_steps=7)
        first = self.create([[reasoning, text, read], [send("done", "send_1")]], configuration=config)
        await first.post_user_message("负责数据 a")
        await first.run_until_settled()
        before = first.state.history.snapshot()
        await first.close()
        sent = []
        requests = []
        second = self.resume(first.thread_id, [[send("continued", "send_2")]],
                             send_message=sent.append,
                             trace=lambda kind, data: requests.append((kind, data)))
        self.assertEqual(second.thread_id, first.thread_id)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(second.configuration, config)
        self.assertEqual(second.process_manager.workspace, self.workspace)
        self.assertEqual(second.state.history.snapshot()[:-1], before)
        self.assertEqual(second.state.step_number, 2)
        self.assertEqual(sent, [])  # Old answers are not redelivered.
        await second.run_until_idle()
        self.assertEqual(second.state.step_number, 2)  # Resume alone never calls model.
        await second.post_user_message("还负责数据 b")
        await second.run_until_settled()
        prompt = next(data["prompt"] for kind, data in requests if kind == "model_request")
        self.assertIn("<agent_memory>", prompt["memory"]["text"])
        projected = prompt["input"][:len(before)]
        self.assertEqual(projected[1:], before[1:])
        self.assertIn("<terminal_message>", projected[0]["text"])
        self.assertEqual(json.loads(projected[0]["text"].split("\n")[1])["text"], before[0]["text"])
        self.assertEqual(json.loads(prompt["input"][-1]["text"].split("\n")[1])["text"], "还负责数据 b")
        self.assertIn("session_resumed", prompt["input"][-2]["text"])
        self.assertEqual(sent, ["continued"])
        await second.close()
        third = self.resume(first.thread_id)
        self.assertEqual(third.state.step_number, 3)
        self.assertEqual(third.state.history.snapshot()[:-1], second.state.history.snapshot())

    async def test_separate_threads_do_not_share_history(self):
        first = self.create([[send("a", "send_a")]])
        await first.post_user_message("private a")
        await first.run_until_idle()
        await first.close()
        second = self.create()
        self.assertNotEqual(first.thread_id, second.thread_id)
        initial = second.state.history.snapshot()
        self.assertEqual(initial, [])
        self.assertNotIn("private a", str(initial))
        self.assertEqual(len(self.manager.list_threads()), 2)

    async def test_exclusive_lock_is_released_after_close(self):
        first = self.create()
        with self.assertRaisesRegex(ValueError, "already open"):
            self.manager.resume(ScriptedClient([]), first.thread_id)
        # Listing works while a Thread is open, without taking ownership.
        self.assertEqual(self.manager.list_threads()[0]["thread_id"], first.thread_id)
        await first.close()
        resumed = self.resume(first.thread_id)
        await resumed.close()

    async def test_process_is_not_restored_but_timer_registration_is(self):
        first = self.create()
        result = await first.router.registry.execute_async("exec_command", {
            "cmd": "sleep 30", "yield_time_ms": 250, "notify_on_completion": True})
        handle = json.loads(result)["session_id"]
        await first.router.registry.execute_async("create_timer", {"delay_ms": 30000, "message": "later"})
        await first.close()
        second = self.resume(first.thread_id)
        self.assertEqual(second.process_manager.processes, {})
        self.assertEqual({r["kind"] for r in second.generator_manager.list()}, {"timer", "command_completion"})
        self.assertTrue(second.input_queue.empty())
        with self.assertRaisesRegex(ValueError, "Unknown process"):
            await second.process_manager.write_stdin(handle)

    async def test_pending_events_survive_clean_resume(self):
        first = self.create()
        await first.post_user_message("not processed")
        await first.close()
        second = self.resume(first.thread_id)
        self.assertEqual(second.input_queue.snapshot()[0]["item"]["text"], "not processed")

    async def test_failed_session_cannot_resume_and_tools_are_not_replayed(self):
        first = self.create()  # Exhausted model fails after consuming user event.
        await first.post_user_message("work")
        with self.assertRaisesRegex(RuntimeError, "script exhausted"):
            await first.run_until_idle()
        self.assertTrue(first.rollout.closed)
        with self.assertRaisesRegex(ValueError, "did not close cleanly"):
            self.manager.resume(ScriptedClient([]), first.thread_id)

    async def test_unclean_writer_does_not_resume_or_truncate(self):
        first = self.create()
        path = first.rollout.path
        first.rollout.close()  # Simulate process death without session_closed.
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "did not close cleanly"):
            self.manager.resume(ScriptedClient([]), first.thread_id)
        self.assertEqual(path.read_bytes(), before)

    async def test_truncated_or_corrupt_rollout_rejected_without_changes(self):
        first = self.create()
        await first.close()
        path = first.rollout.path
        original = path.read_bytes()
        for suffix in (b'{"type":', b'not-json\n'):
            path.write_bytes(original + suffix)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                self.manager.resume(ScriptedClient([]), first.thread_id)
            self.assertEqual(path.read_bytes(), before)

    async def test_unpaired_tool_call_rejected_even_with_clean_marker(self):
        first = self.create()
        first.record_items([{"type": "function_call", "name": "exec_command",
                             "call_id": "unfinished", "arguments": '{"cmd":"echo unsafe"}'}])
        await first.close()
        with self.assertRaisesRegex(ValueError, "Unfinished stored tool calls"):
            self.manager.resume(ScriptedClient([]), first.thread_id)

    async def test_persistence_failure_prevents_accepting_new_event(self):
        first = self.create()
        first.rollout.failed = True
        with self.assertRaisesRegex(RuntimeError, "writer is closed or failed"):
            await first.post_user_message("must not be accepted")
        self.assertTrue(first.input_queue.empty())

    async def test_failed_fsync_is_not_marked_as_a_clean_shutdown(self):
        first = self.create()
        with patch("agent_harness.core.rollout.recorder.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                await first.post_user_message("not acknowledged")
        self.assertTrue(first.rollout.failed)
        self.assertTrue(first.input_queue.empty())
        await first.close()
        with self.assertRaisesRegex(ValueError, "did not close cleanly"):
            self.manager.resume(ScriptedClient([]), first.thread_id)

    async def test_cleanup_failure_cannot_leave_a_clean_checkpoint(self):
        first = self.create()
        original = first.process_manager.close
        async def fail_cleanup():
            raise OSError("cleanup failed")
        first.process_manager.close = fail_cleanup
        with self.assertRaisesRegex(OSError, "cleanup failed"):
            await first.close()
        first.process_manager.close = original
        with self.assertRaisesRegex(ValueError, "did not close cleanly"):
            self.manager.resume(ScriptedClient([]), first.thread_id)

    async def test_invalid_id_and_missing_workspace_do_not_mutate_rollout(self):
        for thread_id in ("../escape", "not-a-uuid", "00000000-0000-0000-0000-00000000000Z"):
            with self.assertRaises(ValueError):
                self.manager.resume(ScriptedClient([]), thread_id)
        first = self.create()
        await first.close()
        original = first.rollout.path.read_bytes()
        self.workspace.rename(self.root / "moved-workspace")
        with self.assertRaises(OSError):
            self.manager.resume(ScriptedClient([]), first.thread_id)
        self.assertEqual(first.rollout.path.read_bytes(), original)

    async def test_close_during_active_step_drains_before_checkpoint(self):
        first = self.create([[send("done", "send_1")]])
        await first.post_user_message("work")
        worker = asyncio.create_task(first.run())
        await asyncio.sleep(0)
        await first.close()
        await worker
        second = self.resume(first.thread_id)
        self.assertEqual(second.state.step_number, 1)
        self.assertIn("done", json.dumps(second.state.history.snapshot()))


class PersistentCliTests(unittest.TestCase):
    def test_demo_create_list_resume_across_processes_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("demo\n", encoding="utf-8")
            base = [sys.executable, "-m", "agent_harness", "--thread-dir", str(root / "threads")]
            def run(*args):
                return subprocess.run(base + list(args), text=True, capture_output=True, timeout=10)
            first = run("--demo", "--workspace", str(workspace))
            self.assertEqual(first.returncode, 0, first.stderr)
            thread_id = next(line.removeprefix("Thread: ") for line in first.stdout.splitlines()
                             if line.startswith("Thread: "))
            listed = run("--list-threads")
            self.assertEqual(json.loads(listed.stdout)[0]["thread_id"], thread_id)
            second = run("--demo", "--resume", thread_id)
            self.assertEqual(second.returncode, 0, second.stderr)
            recorder, history = RolloutRecorder.resume(root / "threads", thread_id)
            try:
                self.assertEqual(history.step_number, 6)
                self.assertEqual(sum(item.get("type") == "function_call_output" for item in history.items), 6)
            finally:
                recorder.close()
            invalid = run("--demo", "--resume", thread_id, "--workspace", str(workspace))
            self.assertNotEqual(invalid.returncode, 0)
