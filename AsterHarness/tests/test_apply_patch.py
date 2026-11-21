"""Codex fixture compatibility and our workspace/API/event-runtime boundaries."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.apply_patch import apply_patch
from agent_harness.apply_patch.file_update import seek_sequence
from agent_harness.apply_patch.parser import parse_patch
from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session
from agent_harness.core.session.step import build_prompt
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools


FIXTURES = Path(__file__).parent / "fixtures/apply_patch/codex_scenarios.json"


class PatchEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8"))
        return path

    def test_add_update_delete_move_and_summary(self):
        self.write("old.txt", "old\n")
        self.write("delete.txt", "bye\n")
        changes = apply_patch("""*** Begin Patch
*** Add File: docs/note.md
+你好
*** Update File: old.txt
*** Move to: new/name.txt
@@
-old
+new
*** Delete File: delete.txt
*** End Patch""", self.root)
        self.assertEqual([(c.kind, c.path, c.move_path) for c in changes],
                         [("A", "docs/note.md", None), ("M", "old.txt", "new/name.txt"),
                          ("D", "delete.txt", None)])
        self.assertEqual((self.root / "docs/note.md").read_text(), "你好\n")
        self.assertEqual((self.root / "new/name.txt").read_text(), "new\n")
        self.assertFalse((self.root / "old.txt").exists())
        self.assertFalse((self.root / "delete.txt").exists())

    def test_failed_update_keeps_original_and_explains_context(self):
        path = self.write("a.txt", "actual\n")
        with self.assertRaisesRegex(ValueError, "Failed to find expected lines"):
            apply_patch("*** Begin Patch\n*** Update File: a.txt\n@@\n-wrong\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"actual\n")

    def test_later_bad_chunk_does_not_partially_write_same_file(self):
        path = self.write("a", "first\nlast\n")
        with self.assertRaisesRegex(ValueError, "expected lines"):
            apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-first\n+changed\n@@\n-missing\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"first\nlast\n")

    def test_failed_write_can_leave_partial_target_and_prior_success(self):
        original_write = Path.write_bytes

        def disk_full(path, contents):
            if path.name == "b":
                original_write(path, b"PARTIAL")
                raise OSError("simulated ENOSPC")
            return original_write(path, contents)

        with patch.object(Path, "write_bytes", disk_full):
            with self.assertRaisesRegex(ValueError, "failed write may also have modified"):
                apply_patch("*** Begin Patch\n*** Add File: a\n+done\n*** Add File: b\n+complete\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_bytes(), b"done\n")
        self.assertEqual((self.root / "b").read_bytes(), b"PARTIAL")

    def test_move_source_removal_failure_is_reported_without_false_rollback(self):
        source = self.write("old", "old\n")
        original_unlink = Path.unlink

        def denied(path, *args, **kwargs):
            if path == source:
                raise PermissionError("simulated source removal failure")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", denied):
            with self.assertRaisesRegex(ValueError, "simulated source removal failure"):
                apply_patch("*** Begin Patch\n*** Update File: old\n*** Move to: new\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(source.read_bytes(), b"old\n")
        self.assertEqual((self.root / "new").read_bytes(), b"new\n")

    def test_invalid_utf8_update_is_rejected_without_overwriting_bytes(self):
        path = self.root / "a"
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(ValueError):
            apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"\xff\xfe")

    def test_parse_all_before_writing_but_later_io_failure_keeps_earlier_changes(self):
        with self.assertRaises(ValueError):
            apply_patch("*** Begin Patch\n*** Add File: a\n+ok\n*** Unknown: b\n*** End Patch", self.root)
        self.assertFalse((self.root / "a").exists())
        with self.assertRaisesRegex(ValueError, "Earlier changes are kept"):
            apply_patch("*** Begin Patch\n*** Add File: a\n+ok\n*** Delete File: missing\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_text(), "ok\n")

    def test_update_chunk_anchor_and_end_of_file(self):
        path = self.write("a", "def first():\n    old\ndef second():\n    old\n")
        apply_patch("*** Begin Patch\n*** Update File: a\n@@ def second():\n-    old\n+    new\n*** End of File\n*** End Patch", self.root)
        self.assertEqual(path.read_text(), "def first():\n    old\ndef second():\n    new\n")

    def test_fuzzy_context_does_not_rewrite_untouched_indentation_or_endings(self):
        path = self.write("a", "  header  \r\nold\nfooter\r")
        apply_patch("*** Begin Patch\n*** Update File: a\n@@\n header\n-old\n+new\n footer\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"  header  \r\nnew\r\nfooter\r")

    def test_unicode_context_normalization(self):
        path = self.write("a", "title — “hello”\nold\n")
        apply_patch('*** Begin Patch\n*** Update File: a\n@@ title - "hello"\n-old\n+new\n*** End Patch', self.root)
        self.assertEqual(path.read_text(), "title — “hello”\nnew\n")

    def test_unicode_line_separators_are_content_not_patch_boundaries(self):
        text = "甲\u2028乙\u0085丙"
        apply_patch(f"*** Begin Patch\n*** Add File: a\n+{text}\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_text(), text + "\n")
        apply_patch(f"*** Begin Patch\n*** Update File: a\n@@\n-{text}\n+新\u2028行\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_text(), "新\u2028行\n")

    def test_exact_match_precedes_fuzzy_match(self):
        self.assertEqual(seek_sequence([" old", "old"], ["old"], 0), 1)
        self.assertIsNone(seek_sequence(["x"], ["x", "y"], 0))

    def test_absolute_inside_paths_and_same_path_move(self):
        path = self.write("a", "old\n")
        apply_patch(f"*** Begin Patch\n*** Update File: {path}\n*** Move to: {path}\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_text(), "new\n")

    def test_outside_workspace_paths_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "a"
            outside.write_text("untouched\n")
            (self.root / "link").symlink_to(other, target_is_directory=True)
            for name in (str(outside), "../outside.txt", "link/a", str(self.root)):
                with self.subTest(path=name), self.assertRaises(ValueError):
                    apply_patch(f"*** Begin Patch\n*** Add File: {name}\n+bad\n*** End Patch", self.root)
            self.assertEqual(outside.read_text(), "untouched\n")
        self.write("a", "unchanged\n")
        (self.root / "alias").symlink_to("a")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            apply_patch("*** Begin Patch\n*** Delete File: alias\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_text(), "unchanged\n")

    def test_move_destination_cannot_escape_workspace(self):
        path = self.write("a", "old\n")
        with self.assertRaises(ValueError):
            apply_patch("*** Begin Patch\n*** Update File: a\n*** Move to: ../outside\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_text(), "old\n")

    def test_delete_is_not_recursive(self):
        self.write("dir/a", "safe\n")
        with self.assertRaises(ValueError):
            apply_patch("*** Begin Patch\n*** Delete File: dir\n*** End Patch", self.root)
        self.assertEqual((self.root / "dir/a").read_text(), "safe\n")

    def test_empty_add_and_eof_insertion(self):
        apply_patch("*** Begin Patch\n*** Add File: a\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_bytes(), b"")
        apply_patch("*** Begin Patch\n*** Update File: a\n@@\n+new\n*** End Patch", self.root)
        self.assertEqual((self.root / "a").read_bytes(), b"new\n")

    def test_executable_mode_is_preserved_on_update(self):
        path = self.write("a", "old\n")
        path.chmod(0o755)
        apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_invalid_patch_boundaries_chunks_and_limits(self):
        invalid = [None, "", "*** Begin Patch\n*** End Patch", "```\n*** Begin Patch\n*** End Patch\n```",
            "*** Begin Patch\n*** Add File: a\nno plus\n*** End Patch",
            "*** Begin Patch\n*** Update File: a\n@@\n@@\n-old\n+new\n*** End Patch",
            "*** Begin Patch\n*** Update File: a\n*** Move to: b\n*** End Patch",
            "*** Begin Patch\n*** Add File: \n+x\n*** End Patch",
            "*** Begin Patch\n*** Environment ID: x\n*** Add File: a\n+x\n*** End Patch",
            "x" * (1024 * 1024 + 1)]
        for value in invalid:
            with self.subTest(patch=str(value)[:80]), self.assertRaises(ValueError):
                parse_patch(value)
        many = "*** Begin Patch\n" + "*** Add File: a\n+x\n" * 101 + "*** End Patch"
        with self.assertRaisesRegex(ValueError, "100 file"):
            parse_patch(many)

    def test_binary_and_oversized_updates_rejected_without_changes(self):
        path = self.write("a", "x\x00y\n")
        with self.assertRaisesRegex(ValueError, "binary"):
            apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-x\n+y\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"x\x00y\n")
        self.write("a", "old\n")
        with patch("agent_harness.apply_patch.MAX_FILE_BYTES", 3):
            with self.assertRaisesRegex(ValueError, "8 MiB"):
                apply_patch("*** Begin Patch\n*** Update File: a\n@@\n-old\n+new\n*** End Patch", self.root)
        self.assertEqual(path.read_bytes(), b"old\n")
        with self.assertRaisesRegex(ValueError, "binary"):
            apply_patch("*** Begin Patch\n*** Add File: binary\n+x\x00y\n*** End Patch", self.root)
        self.assertFalse((self.root / "binary").exists())


class CodexFixtureTests(unittest.TestCase):
    """Upstream cases assert final filesystem state, including partial failures."""

    setUp = PatchEngineTests.setUp
    write = PatchEngineTests.write


def fixture_test(case):
    def run(self):
        for name, value in case["input"].items():
            if value is None:
                (self.root / name).mkdir(parents=True, exist_ok=True)
            else:
                self.write(name, value)
        try:
            apply_patch(case["patch"], self.root)
        except ValueError:
            pass  # Upstream scenarios specify final state, not exit status.
        actual = {str(p.relative_to(self.root)): None if p.is_dir()
                  else p.read_bytes().decode("utf-8") for p in self.root.rglob("*")}
        self.assertEqual(actual, case["expected"], case["name"])
    return run


for case in json.loads(FIXTURES.read_text(encoding="utf-8")):
    setattr(CodexFixtureTests, "test_codex_" + case["name"], fixture_test(case))


class PatchToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def call(self, patch_text, ident="edit"):
        return {"type": "function_call", "call_id": ident, "name": "apply_patch",
                "arguments": json.dumps({"patch": patch_text})}

    async def test_success_and_error_are_ordinary_tool_results(self):
        calls = [self.call("*** Begin Patch\n*** Add File: a\n+old\n*** End Patch"),
                 self.call("*** Begin Patch\n*** Update File: a\n@@\n-wrong\n+new\n*** End Patch", "bad")]
        client = ScriptedClient([[call] for call in calls] + [[{
            "type": "function_call", "call_id": "reply", "name": "send_user_message",
            "arguments": '{"text": "done", "wake_model": false}'}]])
        traces = []
        session = Session(client, build_file_tools(self.root), workspace=self.root,
                          trace=lambda event, data: traces.append((event, data)))
        self.addAsyncCleanup(session.close)
        await session.post_user_message("edit")
        await session.run_until_idle()
        results = [i for i in session.state.history.snapshot() if i.get("type") == "function_call_output"]
        self.assertIn("Success. Updated", results[0]["output"])
        self.assertIn("A a", results[0]["output"])
        self.assertIn("Failed to find expected lines", results[1]["output"])
        self.assertEqual((self.root / "a").read_text(), "old\n")
        self.assertEqual(session.state.step_number, 3)
        queued = [d["queued_event"]["kind"] for e, d in traces if e == "event_enqueued"]
        self.assertEqual(queued, ["user_message", "tool_result", "tool_result"])

    async def test_resume_does_not_reapply_old_patch_and_registers_current_tool(self):
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([]), self.root)
        self.addAsyncCleanup(first.close)
        call = self.call("*** Begin Patch\n*** Add File: a\n+original\n*** End Patch")
        first.record_items([call])
        result = await first.router.dispatch_async(call)
        first.record_items([result])
        await first.suspend()
        (self.root / "a").write_text("external change\n")
        second = manager.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(second.close)
        self.assertEqual((self.root / "a").read_text(), "external change\n")
        self.assertTrue(second.input_queue.empty())
        spec = next(t for t in build_prompt(second).tools if t["name"] == "apply_patch")
        self.assertEqual(spec["type"], "function")
        self.assertEqual(spec["parameters"]["required"], ["patch"])
        self.assertIn("JSON function", spec["description"])
