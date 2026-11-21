"""Default prompt and tool documentation must match the event-runtime contract."""

import tempfile
import unittest
from pathlib import Path

from agent_harness.core.client import ScriptedClient
from agent_harness.core.session import Session, SessionConfiguration
from agent_harness.core.session.step import build_prompt
from agent_harness.core.session.session import CHANNEL_INSTRUCTIONS, EVENT_INSTRUCTIONS
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools


class InstructionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = Session(ScriptedClient([]), build_file_tools(self.root), workspace=self.root)
        self.addAsyncCleanup(self.session.close)

    async def test_markdown_is_default_and_runtime_contract_is_present(self):
        source = Path(__file__).resolve().parents[1] / "agent_harness/core/prompt.md"
        instructions = build_prompt(self.session).base_instructions
        self.assertEqual(instructions, source.read_text(encoding="utf-8") + "\n\n" + EVENT_INSTRUCTIONS
                         + "\n\n" + CHANNEL_INSTRUCTIONS)
        overview = instructions.split("# Working principles", 1)[0]
        for phrase in ("Your purpose is to serve the user", "user-defined identity and responsibilities",
                       "take precedence over your own plans", "system rules and authorization boundaries"):
            self.assertIn(phrase, overview)
        for phrase in ("# Communication channels", "<terminal_message>",
                       "send_user_message(text)", "not shown to the user"):
            self.assertIn(phrase, overview)
        for phrase in ("no turns", "all currently queued events", "in a later step",
                       "send_user_message(text)", "NOT delivered", "waits alive",
                       "event generator manager", "<harness_event>", "In-memory Sessions"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)
        self.assertNotIn("yield_time_ms", instructions)  # Tool-specific details stay in schemas.

    async def test_configuration_override_still_replaces_default(self):
        session = Session(ScriptedClient([]), build_file_tools(self.root),
                          configuration=SessionConfiguration(base_instructions="Custom contract"),
                          workspace=self.root)
        self.addAsyncCleanup(session.close)
        self.assertEqual(build_prompt(session).base_instructions, "Custom contract\n\n" + EVENT_INSTRUCTIONS
                         + "\n\n" + CHANNEL_INSTRUCTIONS)

    async def test_generator_sections_are_nested_under_one_runtime_heading(self):
        persistent = ThreadManager(self.root / "threads").create(ScriptedClient([]), self.root)
        self.addAsyncCleanup(persistent.close)
        for session in (self.session, persistent):
            instructions = build_prompt(session).base_instructions
            self.assertEqual(instructions.count("# Event-driven runtime\n"), 1)
            parents, headings, in_code = {}, {}, False
            for line in instructions.splitlines():
                if line.startswith("```"):
                    in_code = not in_code
                if in_code or not line.startswith("#"):
                    continue
                level = len(line) - len(line.lstrip("#"))
                if not line[level:].startswith(" "):
                    continue
                title = line[level:].strip()
                parents = {depth: name for depth, name in parents.items() if depth < level}
                headings[title] = (level, dict(parents))
                parents[level] = title
            for title in ("Responsibilities and lifecycle", "Event delivery and waiting",
                          "Command-completion generators", "Timer generators",
                          "Agent-managed program generators"):
                self.assertEqual(headings[title], (3, {
                    1: "Event-driven runtime", 2: "Event generators and their manager"}))
            if session.rollout is not None:
                self.assertEqual(headings["Persistence and resume"], (3, {
                    1: "Event-driven runtime", 2: "Event generators and their manager"}))
            else:
                self.assertNotIn("Persistence and resume", headings)

    async def test_all_tools_and_parameters_are_documented_without_new_capabilities(self):
        specs = {spec.get("name", spec["type"]): spec for spec in build_prompt(self.session).tools}
        self.assertEqual(set(specs), {"list_directory", "read_file", "send_user_message", "wait_for_event",
                                     "exec_command", "write_stdin", "list_processes", "stop_process", "create_timer", "agent_time",
                                     "list_event_generators", "cancel_event_generator",
                                     "register_program_generator", "restart_program_generator",
                                     "test_program_generator", "apply_patch", "query_history",
                                     "compact_history", "query_compaction", "query_history_directory",
                                     "read_compaction_range", "search_history",
                                     "manage_tool_group", "manage_custom_tool", "tool_search",
                                     "view_image", "read_tool_output", "skills"})
        for name, spec in [(member.get("name", member["type"]), member) for entry in specs.values()
                           for member in (entry["tools"] if entry["type"] == "namespace" else [entry])]:
            self.assertTrue(spec["description"].strip())
            for parameter, schema in spec["parameters"]["properties"].items():
                with self.subTest(tool=name, parameter=parameter):
                    self.assertTrue(schema["description"].strip())
        timer = specs["create_timer"]["parameters"]
        self.assertEqual(set(timer["properties"]),
                         {"delay_ms", "at", "message", "interval_ms", "repeat_count", "name", "notes"})
        self.assertEqual(timer["required"], ["message"])
        notify = specs["exec_command"]["parameters"]["properties"]["notify_on_completion"]
        self.assertIn("does not consume output", notify["description"])
        self.assertIn("no notification was registered", notify["description"])
        self.assertIn("already enqueued", specs["cancel_event_generator"]["description"])
