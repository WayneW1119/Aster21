"""Behavior compatibility against the pre-refactor runtime, not its internals."""

import difflib
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_harness.core.context_manager.agent_context import MEMORY_WORK_GUIDANCE


class RuntimeContractTests(unittest.TestCase):
    def test_pre_refactor_prompts_events_history_and_rollout_remain_identical(self):
        project = Path(__file__).resolve().parents[1]
        fixture = project / "tests/fixtures"
        expected = gzip.decompress((fixture / "runtime_contract_v1.json.gz").read_bytes()).decode()
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.run([sys.executable, str(fixture / "runtime_contract.py")],
                cwd=directory, env={**os.environ, "PYTHONPATH": str(project)},
                text=True, capture_output=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        # The intentional additions to old empty-catalog requests are the two
        # management APIs, dedicated skills namespace and discovery entrypoint. Compare all other content to
        # the original fixture rather than replacing that fixture with new output.
        def without_custom_management(value):
            # Intentional prompt-only addition: retain the old prompt comparison
            # everywhere except the exact new memory work guidance block.
            if isinstance(value, str):
                value = value.replace("\n" + MEMORY_WORK_GUIDANCE, "")
                # Search completion/time-budget wording intentionally changes;
                # navigation tests cover its semantics. Keep the old runtime
                # fixture checking every other instruction/schema/event field.
                value = value.replace(
                    "occurred. Search reports complete and stop_reason; it scans the full scope subject\n"
                    "to match/output limits and a soft time budget. Follow next_cursor, including after\n"
                    "empty incomplete pages, until complete=true when completeness matters. Recent work\n"
                    "is easier to access because it is visible; older work is still archived and\n"
                    "retrievable. Preserve distinctive names,",
                    "occurred. Follow next_cursor, including after empty pages, until the fixed snapshot\n"
                    "is exhausted when completeness matters. Recent work is easier to access because it\n"
                    "is visible; older work is still archived and retrievable. Preserve distinctive names,")
                value = value.replace(
                    "query must stay unchanged. Searches the full frozen scope until enough matches, "
                    "the output budget, or a soft one-second scan budget is reached. Returns complete, "
                    "stop_reason (complete/match_limit/output_budget/time_budget), scanned_count for this "
                    "page and total_count for the scope. An empty incomplete page is NOT an exhaustive "
                    "negative; follow next_cursor.",
                    "query must stay unchanged. Scans at most 512 records/page; an empty page may still "
                    "have a next_cursor.")
                # Approved wording changes describe the built-in channel without
                # assuming it is the only channel. Preserve the original fixture.
                value = value.replace("This is the basic built-in communication channel; "
                    "use the corresponding tools for other connected channels. ",
                    "Currently this is the only user-visible output channel. ")
                value = value.replace("\n\nThe harness directly supports this basic channel. "
                    "Additional channels may be connected through tools and event sources; "
                    "use their actual integrations when available.", "")
                value = value.replace("Terminal messaging and send_user_message are the harness's "
                    "basic built-in communication channel.", "User messages currently come from the terminal.")
                value = value.replace("# Built-in communication channel", "# Terminal communication channel")
                return value.replace("The harness directly supports terminal messages and send_user_message as its basic\n"
                    "communication channel. Other channels may be connected through tools and event\n"
                    "sources; use their actual integrations and instructions when available.\n"
                    "Human messages delivered through the basic channel are presented as",
                    "Human messages currently arrive from the terminal. They are presented as")
            if isinstance(value, list):
                # New durable Step boundaries are storage-only evidence. Their
                # crash/queue semantics have dedicated killed-process tests.
                return [without_custom_management(item) for item in value
                        if not (isinstance(item, dict) and set(item) == {"type", "timestamp", "payload"}
                                and (item["type"] == "step_boundary" or item["type"] == "runtime_checkpoint"
                                     and "boundary" in item["payload"]))]
            if isinstance(value, dict):
                if value.get("event") == "model_request" and isinstance(value.get("prompt"), dict):
                    # Intentional transport-only identity; rendered instructions,
                    # input, tools and archived runtime behavior still match v1.
                    # Its routing and resume invariants are tested at HTTP boundary.
                    value = {**value, "prompt": {key: content for key, content in value["prompt"].items()
                                               if key != "cache_session_id"}}
                if value.get("type") == "function" and value.get("name") in {"register_program_generator", "create_timer"}:
                    schema = value["parameters"]
                    for optional in ("name", "notes"):
                        schema["properties"].pop(optional, None)
                    if value["name"] == "create_timer":
                        schema["properties"].pop("at", None)
                        schema["required"] = ["delay_ms", "message"]
                        schema["properties"]["delay_ms"]["description"] = "Delay before the first event in milliseconds; at least 1. Relative delays only, not an absolute timestamp or cron expression."
                if value.get("type") == "function" and value.get("name") == "exec_command":
                    value["description"] = value["description"].replace(
                        "Commands use the latest host-configured sandbox mode and permissions; consult the current environment context.",
                        "Commands run with host permissions, without a sandbox.")
                if value.get("type") == "function" and value.get("name") == "write_stdin":
                    # Intentional addition: non-TTY handles now accept Ctrl-C.
                    value["parameters"]["properties"]["chars"]["description"] = (
                        "Text to write to stdin; requires tty=true on the original command. "
                        "Defaults to empty, which polls without writing. A standalone Ctrl-C "
                        "sends SIGINT to the command's process group.")
                if value.get("type") == "function" and value.get("name") == "send_user_message":
                    # The old fixture used an invalid strict schema with optional
                    # wake_model. Compare all fields except this intentional correction.
                    value = {**value, "strict": True}
                return {key: without_custom_management([
                    item for item in content if not (isinstance(item, dict)
                    and (item.get("type") == "namespace" and item.get("name") == "skills"
                         or item.get("type") == "tool_search" or item.get("type") == "function"
                    and item.get("name") in {"manage_tool_group", "manage_custom_tool",
                                             "agent_time", "view_image", "read_tool_output", "list_processes", "stop_process"}))])
                    if key == "tools" and isinstance(content, list)
                    else without_custom_management(content) for key, content in value.items()}
            return value

        actual = json.dumps(without_custom_management(json.loads(process.stdout)),
                            ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if actual != expected:
            difference = "".join(difflib.unified_diff(expected.splitlines(True),
                actual.splitlines(True), fromfile="pre-refactor", tofile="current"))
            self.fail("Runtime behavior changed:\n" + difference[:12000])
        self.assertEqual(set(json.loads(process.stdout)),
            {"memory_compact_resume", "ingress_errors_plain_text", "context_budget"})
