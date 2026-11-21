"""Snapshot directory addressing, native transport and bounded archival recall."""

from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_harness.core.client import ResponsesClient, to_api_item, order_tool_outputs
from agent_harness.core.client_common import Prompt
from agent_harness.core.context_manager import ContextManager
from agent_harness.core.context_manager.navigation import HistoryNavigator, HistoryView
from agent_harness.core.history_render import numbered_input
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.session.step import run_step
from agent_harness.core.thread_manager import ThreadManager
from test_memory_context import RecordingClient, call


def message(text):
    return {"type": "message", "role": "user", "text": text}


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.history = ContextManager()
        self.history.record_items([message("early CEDAR-key fact"), message("second fact"), message("recent fact")])
        self.view = HistoryView(tuple(self.history.ids()), self.history.generation)
        self.nav = HistoryNavigator(self.history, lambda: self.view, "scope-a")

    def compact(self, start, end, summary="stage handoff"):
        op = self.history.prepare_compaction(start, end, self.history.generation, summary)
        self.history.apply_compaction(op)
        self.view = HistoryView(tuple(self.history.ids()), self.history.generation)
        return op["summary_id"]

    def test_number_id_and_stable_root_path_resolve_same_record(self):
        first = self.compact("h000000000001", "h000000000002")
        second = self.compact(first, "h000000000003")
        view_id = json.loads(self.nav.directory())["view_id"]
        for target in ("1-1-2", second + "-1-2", "h000000000002"):
            self.assertEqual(self.nav.resolve(target, view_id), "h000000000002")
        self.assertEqual(self.nav.resolve(1, view_id), second)

    def test_bad_paths_and_no_active_view_rejected(self):
        for target in (0, True, "0", "4", "1-1", "h999", "1--2", [], "1-0"):
            with self.subTest(target=target), self.assertRaises((ValueError, TypeError)):
                self.nav.resolve(target)
        self.view = None
        with self.assertRaises(ValueError):
            self.nav.resolve("1")
        self.assertEqual(self.nav.resolve("h000000000001"), "h000000000001")

    def test_structure_lists_only_summaries_with_stable_ids_and_positions(self):
        first = self.compact("h000000000001", "h000000000002")
        second = self.compact(first, "h000000000003")
        info = json.loads(self.nav.structure("1", view_id=json.loads(self.nav.directory())["view_id"]))
        self.assertEqual(info, {"id": second, "item_count": 2, "original_item_count": 3,
            "max_depth": 2, "compact_items": [{"position": 1, "id": first, "original_item_count": 2}],
            "next_compact_offset": None})
        self.assertNotIn("source_ids", info)

    def test_structure_paginates_compact_children_without_plain_id_dump(self):
        a = self.compact("h000000000001", "h000000000001")
        b = self.compact("h000000000002", "h000000000002")
        root = self.compact(a, "h000000000003")
        first = json.loads(self.nav.structure(root, limit=1))
        second = json.loads(self.nav.structure(root, offset=first["next_compact_offset"], limit=1))
        self.assertEqual(first["compact_items"][0]["id"], a)
        self.assertEqual(second["compact_items"][0]["id"], b)

    def test_children_and_originals_have_distinct_ordered_sequences(self):
        first = self.compact("h000000000001", "h000000000002")
        root = self.compact(first, "h000000000003")
        direct = json.loads(self.nav.range(root, "children", 1, 2))
        originals = json.loads(self.nav.range(root, "originals", 1, 3))
        self.assertEqual([i["id"] for i in direct["items"]], [first, "h000000000003"])
        self.assertEqual([i["id"] for i in originals["items"]],
                         ["h000000000001", "h000000000002", "h000000000003"])
        self.assertNotIn("history_summary", originals["items"][0]["content"])

    def test_deep_comb_tree_can_be_read_and_searched_without_recursion(self):
        root = "h000000000001"
        for _ in range(1200):
            root = self.compact(root, root, "rolled stage")
        self.assertEqual(self.history.depth(root), 1200)
        result = json.loads(self.nav.range(root, "originals", 1, 1))
        self.assertEqual(result["items"][0]["id"], "h000000000001")
        hits = json.loads(self.nav.search("CEDAR-key", root))
        self.assertEqual(hits["matches"][0]["id"], "h000000000001")

    def test_range_unicode_and_escaping_are_lossless_under_whole_result_budget(self):
        text = ('中文🙂\n"\\\t' * 700)
        self.history.record_items([message(text)])
        root = self.compact("h000000000004", "h000000000004")
        collected, position, offset = "", 1, 0
        for _ in range(100):
            raw = self.nav.range(root, "originals", position, 1, offset, 1024)
            self.assertLessEqual(len(raw), 1024)
            page = json.loads(raw)
            entry = page["items"][0]
            self.assertEqual(entry["offset"], len(collected))
            collected += entry["content"]
            if page["next"] is None:
                break
            self.assertEqual(page["next"]["target"], root)
            position, offset = page["next"]["position"], page["next"]["offset"]
        else:
            self.fail("range pagination failed to terminate")
        self.assertEqual(collected, text)

    def test_single_read_returns_stable_identity_not_item_json(self):
        page = json.loads(self.nav.read("1", view_id=json.loads(self.nav.directory())["view_id"]))
        self.assertEqual(page["id"], "h000000000001")
        self.assertEqual(page["items"][0]["content"], "early CEDAR-key fact")
        self.assertNotIn("item_json", page["items"][0])

    def test_search_hidden_original_and_frozen_pagination_does_not_chase_appends(self):
        root = self.compact("h000000000001", "h000000000002", "No lookup key retained")
        page = json.loads(self.nav.search("fact", "archive", limit=1))
        self.history.record_items([message("NEW fact must not extend snapshot")])
        ids = [i["id"] for i in page["matches"]]
        while page["next_cursor"]:
            page = json.loads(self.nav.search("fact", "archive", page["next_cursor"], limit=1))
            ids.extend(i["id"] for i in page["matches"])
        self.assertEqual(ids, ["h000000000001", "h000000000002", "h000000000003"])
        hit = json.loads(self.nav.search("cedar-KEY", root))
        self.assertEqual(hit["matches"][0]["id"], "h000000000001")

    def test_search_finds_far_record_without_fixed_scan_window(self):
        self.history.record_items([message("no match") for _ in range(12000)] + [message("far marker")])
        with patch('agent_harness.core.context_manager.navigation.monotonic', return_value=0):
            page = json.loads(self.nav.search("far marker"))
        self.assertEqual(page["matches"][0]["snippet"], "far marker")
        self.assertTrue(page["complete"])
        self.assertEqual(page["stop_reason"], "complete")
        self.assertEqual(page["scanned_count"], 12004)
        self.assertEqual(page["total_count"], 12004)
        self.assertIsNone(page["next_cursor"])

    def test_search_time_budget_empty_page_resumes_without_skipping_records(self):
        with patch('agent_harness.core.context_manager.navigation.monotonic', side_effect=[0, 0, 2]):
            first = json.loads(self.nav.search("recent"))
        self.assertEqual(first["matches"], [])
        self.assertFalse(first["complete"])
        self.assertEqual(first["stop_reason"], "time_budget")
        self.assertEqual(first["scanned_count"], 2)
        self.assertIsNotNone(first["next_cursor"])
        second = json.loads(self.nav.search("recent", cursor=first["next_cursor"]))
        self.assertEqual(second["matches"][0]["snippet"], "recent fact")
        self.assertTrue(second["complete"])
        self.assertEqual(second["scanned_count"], 1)

    def test_search_distinguishes_exhaustive_negative_from_match_limit(self):
        missing = json.loads(self.nav.search("absent"))
        self.assertTrue(missing["complete"])
        self.assertEqual(missing["matches"], [])
        self.assertIsNone(missing["next_cursor"])
        first = json.loads(self.nav.search("fact", limit=1))
        self.assertFalse(first["complete"])
        self.assertEqual(first["stop_reason"], "match_limit")
        self.assertEqual(first["scanned_count"], 1)

    def test_search_budget_and_bad_or_cross_scope_cursors(self):
        self.history.record_items([message('"' * 180 + "needle" + '\\' * 300)] * 12)
        raw = self.nav.search("needle", max_chars=1024)
        self.assertLessEqual(len(raw), 1024)
        page = json.loads(raw)
        self.assertTrue(page["matches"])
        self.assertFalse(page["complete"])
        self.assertEqual(page["stop_reason"], "output_budget")
        cursor = page["next_cursor"]
        for other, query, token in ((self.nav, "different", cursor),
            (HistoryNavigator(self.history, lambda: self.view, "scope-b"), "needle", cursor),
            (self.nav, "needle", "!!!")):
            with self.assertRaises(ValueError):
                other.search(query, cursor=token)
        ids = [item["id"] for item in page["matches"]]
        while page["next_cursor"]:
            raw = self.nav.search("needle", cursor=page["next_cursor"], max_chars=1024)
            self.assertLessEqual(len(raw), 1024)
            page = json.loads(raw)
            ids.extend(item["id"] for item in page["matches"])
        self.assertTrue(page["complete"])
        self.assertEqual(ids, self.history.ids()[3:])

    def test_invalid_range_and_bounds_do_not_mutate_archive(self):
        root = self.compact("h000000000001", "h000000000002")
        before = self.history.snapshot()
        for kwargs in ({"start": 0}, {"end": 99}, {"mode": "bad"},
                       {"offset": -1}, {"max_chars": 100}):
            with self.assertRaises(ValueError):
                self.nav.range(**({"target": root, "mode": "children", "start": 1, "end": 2} | kwargs))
        self.assertEqual(self.history.snapshot(), before)

    def test_directory_pages_are_bounded_and_freeze_across_appends(self):
        self.history.record_items([message('中文🙂"\\' * 90) for _ in range(30)])
        self.view = HistoryView(tuple(self.history.ids()), self.history.generation, self.history.revision)
        original_ids = self.history.ids()
        raw = self.nav.directory(limit=50, max_chars=1024)
        first = page = json.loads(raw)
        self.assertLessEqual(len(raw), 1024)
        self.assertIsNotNone(first["next_offset"])
        self.history.record_items([message("later append")])
        entries = list(page["entries"])
        while page["next_offset"] is not None:
            raw = self.nav.directory(first["view_id"], page["next_offset"], 50, 1024)
            self.assertLessEqual(len(raw), 1024)
            page = json.loads(raw)
            entries.extend(page["entries"])
        self.assertEqual([i["id"] for i in entries], original_ids)
        self.assertEqual([i["position"] for i in entries], list(range(1, 34)))
        self.assertEqual(page["item_count"], 33)

    def test_old_directory_reconstructs_after_nested_replacements(self):
        old = json.loads(self.nav.directory())
        original = self.history.ids()
        first = self.compact(original[0], original[1])
        self.history.record_items([message("new work")])
        self.compact(first, original[-1])
        restored = json.loads(self.nav.directory(old["view_id"]))
        self.assertEqual(restored, old)
        self.assertEqual(self.nav.resolve(2, old["view_id"]), original[1])

    def test_numeric_addresses_need_directory_but_stable_ids_do_not(self):
        for operation in (lambda: self.nav.resolve(1), lambda: self.nav.read("1"),
                          lambda: self.nav.structure("1")):
            with self.assertRaisesRegex(ValueError, "view_id"):
                operation()
        self.assertEqual(self.nav.resolve("h000000000001"), "h000000000001")

    def test_directory_reconstruction_preserves_old_checkpoint_positions(self):
        before = json.loads(self.nav.directory())
        ids = self.history.ids()
        op = self.history.prepare_compaction(ids[0], ids[1], 0, "stage",
            {"type": "message", "role": "user", "text": "<agent_memory>\nlegacy checkpoint\n</agent_memory>"})
        self.history.apply_compaction(op)
        self.view = HistoryView(tuple(self.history.ids()), self.history.generation, self.history.revision)
        with_checkpoint = json.loads(self.nav.directory())
        ids = self.history.ids()
        self.compact(ids[0], ids[-1])
        self.assertEqual(json.loads(self.nav.directory(before["view_id"])), before)
        self.assertEqual(json.loads(self.nav.directory(with_checkpoint["view_id"])), with_checkpoint)

    def test_invalid_directory_scope_revision_generation_and_page_arguments(self):
        token = json.loads(self.nav.directory())["view_id"]
        other = HistoryNavigator(self.history, lambda: self.view, "different-thread")
        with self.assertRaises(ValueError):
            other.directory(token)
        for wrong in ("bad", token.replace("v0", "v9"), token.replace("r3", "r999"),
                      token.replace("v0", "v1"), 1):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.nav.directory(wrong)
        for kwargs in ({"offset": -1}, {"offset": 4}, {"limit": 0}, {"limit": True},
                       {"max_chars": 100}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.nav.directory(**kwargs)
        self.assertEqual(json.loads(self.nav.directory(token, offset=3))["entries"], [])

    def test_empty_directory(self):
        empty = ContextManager()
        nav = HistoryNavigator(empty, lambda: None, "empty")
        page = json.loads(nav.directory())
        self.assertEqual(page["item_count"], 0)
        self.assertEqual(page["entries"], [])
        self.assertIsNone(page["next_offset"])
        self.assertEqual(json.loads(nav.directory(page["view_id"])), page)


class NumberRenderingTests(unittest.TestCase):
    def test_legacy_render_shim_keeps_native_items_without_markers(self):
        c = call("read_file", path="x")
        raw = [c, message("new event"), {"type": "function_call_output", "call_id": c["call_id"], "output": "result"}]
        api = [to_api_item(i) for i in raw]
        ordered = order_tool_outputs(api)
        labels = {id(i): f"[{n}]" for n, i in enumerate(api, 1)}
        result = numbered_input(ordered, [labels[id(i)] for i in ordered])
        self.assertEqual(result, ordered)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], c)
        self.assertEqual(result[1]["output"], "result")
        self.assertEqual(raw[2]["output"], "result")

    def test_opaque_reasoning_is_preserved_not_decoded_or_given_extra_api_fields(self):
        reasoning = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
        result = numbered_input([reasoning], ["[1]"])
        self.assertEqual(result, [reasoning])

    def test_actual_adapter_does_not_render_legacy_labels(self):
        prompt = Prompt("instructions", [message("body")], [], ["[1]"])
        original = deepcopy(prompt)
        response = io.BytesIO(json.dumps({"status": "completed", "output": []}).encode())
        response.headers = {"Content-Type": "application/json"}
        client = ResponsesClient("https://example.invalid/responses", "test", "not-real", stream=False)
        with patch("agent_harness.core.client.urlopen", return_value=response) as mocked:
            client.respond(prompt)
        payload = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(payload["input"], [to_api_item(message("body"))])
        self.assertEqual(prompt, original)
        self.assertNotIn("history_labels", payload)

    def test_actual_wire_preserves_every_native_item_without_appended_directory(self):
        c = call("exec_command", cmd="pwd")
        reasoning = {"type": "reasoning", "summary": [], "encrypted_content": "opaque"}
        items = [message("task"), reasoning,
                 {"type": "message", "role": "assistant", "text": "checking"}, c,
                 {"type": "function_call_output", "call_id": c["call_id"], "output": "raw result"}]
        prompt = Prompt("fixed instructions", items, [])
        original = deepcopy(prompt)
        response = io.BytesIO(json.dumps({"status": "completed", "output": []}).encode())
        response.headers = {"Content-Type": "application/json"}
        client = ResponsesClient("https://example.invalid/responses", "test", "not-real", stream=False)
        with patch("agent_harness.core.client.urlopen", return_value=response) as mocked:
            client.respond(prompt)
        body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(body["input"], [to_api_item(i) for i in items])
        self.assertEqual(prompt, original)


class NumberedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ThreadManager(self.root / "threads")

    async def test_compact_by_directory_numbers_then_resume_and_search_old_fact(self):
        client = RecordingClient([])
        session = self.manager.create(client, self.root)
        session.record_items([message("unique EARLY-CEDAR fact"), message("other detail")])
        view_id = json.loads(session.history_navigation.directory())["view_id"]
        session.client = client = RecordingClient([[call("compact_history", view_id=view_id, start=1, end=2, summary="stage done; no publication")],
                                  [call("send_user_message", text="done", wake_model=False)]])
        await session.post_user_message("compact old history")
        await session.run_until_idle()
        summary = session.state.history.ids()[0]
        self.assertIsNone(client.prompts[1].history_labels)
        self.assertIn(summary, client.prompts[1].input[0]["text"])
        result = next(i for i in client.prompts[1].input if i["type"] == "function_call_output")
        receipt = json.loads(result["output"])
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["directory_view_id"], view_id)
        self.assertIn("fresh", receipt["notice"])
        self.assertNotIn("EARLY-CEDAR", json.dumps(client.prompts[1].input))
        self.assertEqual(session.state.history.generation, 1)
        archive = session.state.history.ids("archive")
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        try:
            hit = json.loads(resumed.history_navigation.search("EARLY-CEDAR"))
            self.assertEqual(hit["matches"][0]["id"], "h000000000001")
            self.assertEqual(resumed.state.history.ids("archive")[:len(archive)], archive)
            self.assertIn("EARLY-CEDAR", resumed.history_navigation.read(hit["matches"][0]["id"]))
        finally:
            await resumed.close()

    async def test_numbers_are_request_snapshot_not_later_live_history(self):
        session = self.manager.create(RecordingClient([]), self.root)
        try:
            session.record_items([message("original")])
            context = StepContext.capture(session)
            session.record_items([message("later")])
            session._history_view = context.history_view
            view_id = json.loads(session.history_navigation.directory())["view_id"]
            self.assertEqual(session.history_navigation.resolve(1, view_id), context.history_view.ids[0])
            with self.assertRaises(ValueError):
                session.history_navigation.resolve(2, view_id)
            session.record_items([call("search_history", query="NEVER-OCCURRED-QUERY")])
            result = json.loads(session.history_navigation.search("NEVER-OCCURRED-QUERY"))
            self.assertEqual(result["matches"], [])
            self.assertEqual(result["snapshot_revision"], context.history_view.revision)
        finally:
            await session.close()

    async def test_search_cursor_survives_resume_and_new_archive_appends(self):
        session = self.manager.create(RecordingClient([]), self.root)
        session.record_items([message("needle a"), message("needle b")])
        first = json.loads(session.history_navigation.search("needle", limit=1))
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        try:
            resumed.record_items([message("needle c")])
            second = json.loads(resumed.history_navigation.search("needle", cursor=first["next_cursor"]))
            self.assertEqual([m["snippet"] for m in second["matches"]], ["needle b"])
        finally:
            await resumed.close()

    async def test_nested_depth_and_original_sequence_rebuild_on_resume(self):
        session = self.manager.create(RecordingClient([]), self.root)
        session.record_items([message("deep old evidence")])
        root = session.state.history.ids()[-1]
        for _ in range(30):
            session._compact_boundary = set(session.state.history.ids())
            outcome = json.loads(session.compact_history(root, root, session.state.history.generation, "stage"))
            root = outcome["summary_id"]
        await session.close()
        resumed = self.manager.resume(RecordingClient([]), session.thread_id)
        try:
            info = json.loads(resumed.history_navigation.structure(root))
            self.assertEqual(info["max_depth"], 30)
            self.assertEqual(info["original_item_count"], 1)
            self.assertIn("deep old evidence", resumed.history_navigation.range(root, "originals", 1, 1))
        finally:
            await resumed.close()

    async def test_directory_memory_preparation_compact_refresh_and_resume(self):
        session = self.manager.create(RecordingClient([]), self.root)
        session.record_items([message("original CEDAR evidence"), message("completed stage")])
        original_ids = session.state.history.ids()
        directory_call = call("query_history_directory", view_id=None, offset=0, limit=50, max_chars=6000)
        session.client = RecordingClient([[directory_call]])
        await session.post_user_message("organize then compact")
        await run_step(session, await session.input_queue.drain(wait=False))
        directory = json.loads(session.input_queue.snapshot()[0]["item"]["output"])
        self.assertEqual(directory["item_count"], 3)
        self.assertNotIn(directory_call["call_id"],
                         [e.get("call_id") for e in directory["entries"]])
        token = directory["view_id"]

        async def step(tool):
            session.client = RecordingClient([[tool]])
            await run_step(session, await session.input_queue.drain(wait=False))
            output = session.input_queue.snapshot()[0]["item"]["output"]
            self.assertNotIn('"error"', output)
            return json.loads(output) if output.startswith("{") else output

        # Preparation after querying the directory must not renumber it.
        await step(call("write_memory", path="index.md", text="Responsibility: maintain ledger. Verified stage."))
        outcome = await step(call("compact_history", view_id=token, start=1, end=2,
                                  summary="Stage verified; original evidence is archived."))
        self.assertEqual(outcome["status"], "completed")
        datetime.fromisoformat(outcome["compacted_at"])
        self.assertEqual(outcome["directory_view_id"], token)
        self.assertIn("fresh", outcome["notice"])
        self.assertEqual(session.state.history.sources(outcome["summary_id"]), original_ids)
        self.assertEqual(json.loads(session.history_navigation.directory(token)), directory)
        self.assertEqual(session.memory.current["text"], session.agent_context.read_memory("index.md"))

        fresh = await step(call("query_history_directory", view_id=None, offset=0, limit=50, max_chars=12000))
        self.assertNotEqual(fresh["view_id"], token)
        self.assertEqual(fresh["entries"][0]["id"], outcome["summary_id"])
        self.assertTrue(fresh["entries"][0]["is_compaction"])
        # Include the old directory call/receipt and memory update in a later
        # cleanup, but leave the current query and compact pairs outside it.
        last = fresh["item_count"]
        compacted = await step(call("compact_history", view_id=fresh["view_id"], start=1, end=last,
                                    summary="Ledger stage complete; responsibilities saved. No publication."))
        self.assertTrue(compacted["memory_baseline_advanced"])
        self.assertFalse(any(i.get("call_id") == directory_call["call_id"]
                             for i in session.state.history.snapshot()))
        session.memory.validate(session.state.history.snapshot(), session.agent_context)
        pending = session.input_queue.snapshot()[0]["item"]
        await session.close()

        resumed = self.manager.resume(RecordingClient([[call("send_user_message", text="done", wake_model=False)]]), session.thread_id)
        try:
            self.assertEqual(resumed.input_queue.snapshot()[0]["item"], pending)
            self.assertEqual(json.loads(resumed.history_navigation.directory(token)), directory)
            self.assertEqual(resumed.history_navigation.resolve(1, token), original_ids[0])
            await resumed.run_until_idle()
            self.assertEqual(resumed.state.history.generation, 2)
            resumed.memory.validate(resumed.state.history.snapshot(), resumed.agent_context)
            self.assertTrue(all(p.history_labels is None for p in resumed.client.prompts))
        finally:
            await resumed.close()

    async def test_stale_directory_is_not_blanket_banned_but_changed_range_is_rejected(self):
        session = self.manager.create(RecordingClient([]), self.root)
        self.addAsyncCleanup(session.close)
        session.record_items([message(str(i)) for i in range(5)])
        token = json.loads(session.history_navigation.directory())["view_id"]
        session._compact_boundary = set(session.state.history.ids())
        ids = session.state.history.ids()
        session.compact_history(ids[1], ids[1], 0, "middle replacement")
        # Endpoints survive, but the interior is no longer the selected records.
        bad = await session.router.invoke_async(call("compact_history", view_id=token,
                                                      start=1, end=3, summary="unsafe"))
        self.assertIn("Selected range changed", bad.output)
        # No forced directory expiry: an untouched range still maps exactly.
        good = await session.router.invoke_async(call("compact_history", view_id=token,
                                                       start=4, end=5, summary="untouched tail"))
        self.assertEqual(json.loads(good.output)["status"], "completed")
        self.assertEqual(session.state.history.generation, 2)
