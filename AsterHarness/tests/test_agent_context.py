import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_harness.core.client import ScriptedClient
from agent_harness.core.context_manager.agent_context import AgentContext
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager


def call(name, arguments, ident):
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments),
            "call_id": ident}


class AgentContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")

    def create(self, responses=(), **kwargs):
        session = self.manager.create(ScriptedClient(responses), self.root, **kwargs)
        self.addAsyncCleanup(session.close)
        return session

    async def write_index(self, session, text):
        session.client = ScriptedClient([
            [call("write_memory", {"path": "index.md", "text": text}, "setup_write")],
            [call("send_user_message", {"text": "saved", "wake_model": False}, "setup_send")]])
        await session.post_user_message("Save test memory")
        await session.run_until_idle()

    async def test_identity_memory_and_tools_are_thread_private(self):
        first = self.create(agent_instructions="Maintain data A.")
        second = self.create(agent_instructions="Maintain data B.")
        await self.write_index(first, "A progress")
        a, b = build_prompt(first), build_prompt(second)
        self.assertIn("Maintain data A.", a.base_instructions)
        self.assertNotIn("Maintain data A.", b.base_instructions)
        self.assertEqual(first.agent_context.read_memory("index.md"), "A progress")
        self.assertNotIn("A progress", a.memory["text"])
        self.assertNotIn("A progress", b.memory["text"])
        self.assertEqual(second.state.history.snapshot(), [])
        self.assertEqual(first.memory.reconstruct(a.input)["text"], "A progress")
        self.assertEqual(len(a.tools), 31)
        self.assertEqual(first.agent_context.root, first.rollout.path.parent)
        for spec in [member for entry in a.tools
                     for member in (entry["tools"] if entry["type"] == "namespace" else [entry])]:
            self.assertTrue(spec["description"])
            for parameter in spec["parameters"]["properties"].values():
                self.assertTrue(parameter["description"])

    async def test_external_memory_edits_do_not_replace_initial_history(self):
        session = self.create()
        context = session.agent_context
        first = build_prompt(session)
        context.write_memory("index.md", "new-index")
        context.instructions_path.write_text("New user identity", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Memory invariant"):
            build_prompt(session)
        self.assertIn("No saved memory yet", first.memory["text"])
        self.assertNotIn("new-index", session.memory.item()["text"])
        self.assertEqual(session.state.history.snapshot(), first.input)
        self.assertEqual(context.read_memory("index.md"), "new-index")
        self.assertTrue(session.input_queue.empty())

    async def test_instruction_rules_and_user_content_have_distinct_sections(self):
        session = self.create()
        text = session.agent_context.instructions_path.read_text(encoding="utf-8")
        self.assertFalse(text.startswith("#"))
        prompt = build_prompt(session).base_instructions
        self.assertIn("# Rules for agent instructions and memory\n", prompt)
        self.assertEqual(prompt.count("# Agent-specific instructions\n"), 1)
        self.assertIn("it is not another set of", prompt)
        self.assertNotIn("\nminal_message>\n", prompt)
        self.assertTrue(prompt.endswith(
            "# Agent-specific instructions\n"
            f"Source: {session.agent_context.instructions_path}\n\n" + text))

    async def test_existing_instruction_headings_are_preserved_on_resume(self):
        text = "# Agent instructions\n\nMaintain data A.\n"
        session = self.create(agent_instructions=text)
        await session.suspend()
        resumed = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(resumed.close)
        self.assertEqual(resumed.agent_context.instructions_path.read_text(encoding="utf-8"), text)
        self.assertTrue(build_prompt(resumed).base_instructions.endswith(text))

    async def test_memory_tool_results_follow_event_loop(self):
        events = []
        session = self.create([
            [call("write_memory", {"path": "index.md", "text": "Work blocked on source A"}, "w")],
            [call("read_memory", {"path": "index.md"}, "r")],
            [call("send_user_message", {"text": "Saved", "wake_model": False}, "s")],
        ], trace=lambda event, data: events.append((event, data)))
        await session.post_user_message("Remember status")
        await session.run_until_idle()
        requests = [d["prompt"] for e, d in events if e == "model_request"]
        self.assertEqual(len(requests), 3)
        self.assertIn("No saved memory yet", requests[0]["memory"]["text"])
        self.assertEqual(requests[1]["memory"], requests[0]["memory"])
        self.assertEqual(json.loads(requests[1]["input"][-2]["arguments"])["text"],
                         "Work blocked on source A")
        self.assertEqual(requests[1]["input"][-1]["type"], "function_call_output")
        self.assertEqual(requests[2]["input"][-1]["output"], "Work blocked on source A")
        self.assertEqual(sum("<agent_memory>" in i.get("text", "")
                             for i in session.state.history.snapshot()), 0)
        self.assertEqual(session.state.sent_messages, ["Saved"])

    async def test_agent_updates_own_memory_then_resumes_without_external_edit(self):
        old = "Downloaded; awaiting validation"
        new = "Validated; awaiting publication"
        session = self.create([
            [call("write_memory", {"path": "index.md", "text": old}, "old_write")],
            [call("send_user_message", {"text": "Old status saved", "wake_model": False}, "old_send")],
            [call("write_memory", {"path": "index.md", "text": new}, "new_write")],
            [call("send_user_message", {"text": "New status saved", "wake_model": False}, "new_send")],
        ])
        await session.post_user_message("Download completed; remember the status")
        await session.run_until_idle()
        self.assertEqual(session.agent_context.read_memory("index.md"), old)
        await session.post_user_message("Validation passed; update your memory")
        await session.run_until_idle()
        self.assertEqual(session.agent_context.read_memory("index.md"), new)
        await session.suspend()
        restored = self.manager.resume(ScriptedClient([]), session.thread_id)
        self.addAsyncCleanup(restored.close)
        prompt = build_prompt(restored)
        self.assertIn("sole", prompt.base_instructions)
        self.assertIn("Users communicate", prompt.base_instructions)
        self.assertIn("No saved memory yet", prompt.memory["text"])
        self.assertEqual(restored.agent_context.read_memory("index.md"), new)
        self.assertNotIn(old, prompt.memory["text"])
        writes = [i for i in restored.state.history.snapshot()
                  if i.get("name") == "write_memory"]
        self.assertEqual([json.loads(i["arguments"])["text"] for i in writes], [old, new])

    async def test_details_are_not_automatically_loaded_and_survive_resume(self):
        first = self.create(agent_instructions="Identity A")
        await self.write_index(first, "See topics/data-a.md")
        first.agent_context.write_memory("topics/data-a.md", "PRIVATE_DETAIL_123")
        await first.suspend()
        second = self.manager.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(second.close)
        prompt = build_prompt(second)
        self.assertIn("Identity A", prompt.base_instructions)
        self.assertNotIn("See topics/data-a.md", prompt.memory["text"])
        self.assertEqual(second.router.registry.execute("read_memory", {"path": "index.md"}),
                         "See topics/data-a.md")
        self.assertNotIn("PRIVATE_DETAIL_123", str(prompt))
        self.assertEqual(second.router.registry.execute("read_memory", {"path": "topics/data-a.md"}),
                         "PRIVATE_DETAIL_123")

    async def test_path_restrictions_and_instruction_preservation(self):
        session = self.create(agent_instructions="Do not edit")
        context = session.agent_context
        for path in ("../instructions.md", "/tmp/other.md", "data.txt", "", "../../other.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                context.write_memory(path, "bad")
        (context.memory_root / "escape.md").symlink_to(context.instructions_path)
        with self.assertRaises(ValueError):
            context.write_memory("escape.md", "bad")
        self.assertEqual(context.instructions(), "Do not edit")

    async def test_memory_limits_do_not_silently_truncate_or_overwrite(self):
        session = self.create()
        context = session.agent_context
        context.write_memory("index.md", "keep")
        with self.assertRaises(ValueError):
            context.write_memory("index.md", "x" * 8001)
        self.assertEqual(context.read_memory("index.md"), "keep")
        with self.assertRaises(ValueError):
            context.write_memory("detail.md", "x" * 32001)
        (context.memory_root / "index.md").write_text("x" * 8001)
        with self.assertRaises(ValueError):
            context.read_memory("index.md")
        with self.assertRaises(ValueError):
            build_prompt(session)

    async def test_memory_root_symlink_cannot_expose_instructions(self):
        session = self.create(agent_instructions="protected")
        context = session.agent_context
        (context.memory_root / "index.md").unlink()
        context.memory_root.rmdir()
        context.memory_root.symlink_to(context.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            context.write_memory("instructions.md", "bad")
        self.assertEqual(context.instructions(), "protected")

    async def test_legacy_thread_adds_files_without_changing_rollout_config(self):
        first = self.create()
        await first.suspend()
        first.agent_context.instructions_path.unlink()
        (first.agent_context.memory_root / "index.md").unlink()
        first.agent_context.memory_root.rmdir()
        second = self.manager.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(second.close)
        self.assertTrue(second.agent_context.instructions_path.is_file())
        self.assertEqual(second.configuration, first.configuration)

    async def test_cross_process_resume_loads_same_identity_and_memory(self):
        first = self.create(agent_instructions="PROCESS_IDENTITY")
        await self.write_index(first, "PROCESS_MEMORY")
        await first.suspend()
        code = """
import asyncio, sys
from pathlib import Path
from agent_harness.core.client import ScriptedClient
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.session.step import build_prompt
s = ThreadManager(Path(sys.argv[1])).resume(ScriptedClient([]), sys.argv[2])
p = build_prompt(s)
assert 'PROCESS_IDENTITY' in p.base_instructions
assert 'PROCESS_MEMORY' not in p.memory['text']
assert s.agent_context.read_memory('index.md') == 'PROCESS_MEMORY'
assert sum('<agent_memory>' in i.get('text', '') for i in s.state.history.snapshot()) == 0
assert s.memory.reconstruct(p.input)['text'] == 'PROCESS_MEMORY'
asyncio.run(s.suspend())
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.manager.root), first.thread_id],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_invalid_initial_instructions_do_not_create_thread(self):
        with self.assertRaises(ValueError):
            self.create(agent_instructions="x" * 32001)
        self.assertEqual(self.manager.list_threads(), [])

    async def test_cli_copies_instructions_and_prints_owned_paths(self):
        source = self.root / "source.md"
        source.write_text("CLI_IDENTITY", encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "agent_harness", "--demo",
                                 "--workspace", str(self.root), "--thread-dir", str(self.manager.root),
                                 "--instructions-file", str(source)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Instructions:", result.stdout)
        self.assertIn("Memory:", result.stdout)
        thread = self.manager.list_threads()[0]
        owned = self.manager.root / thread["thread_id"] / "instructions.md"
        self.assertEqual(owned.read_text(encoding="utf-8"), "CLI_IDENTITY")
        source.write_text("CHANGED_SOURCE", encoding="utf-8")
        self.assertEqual(owned.read_text(encoding="utf-8"), "CLI_IDENTITY")

    async def test_cli_rejects_instruction_replacement_on_resume(self):
        first = self.create()
        await first.suspend()
        result = subprocess.run([sys.executable, "-m", "agent_harness", "--demo",
                                 "--resume", first.thread_id, "--instructions-file", "not-used.md"],
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--instructions-file is for new Threads", result.stderr)


if __name__ == "__main__":
    unittest.main()
