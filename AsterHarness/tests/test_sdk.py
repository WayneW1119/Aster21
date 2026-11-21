"""Public SDK exercises the real persistent scheduler, not a second agent loop."""

import asyncio
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

from agent_harness import (
    Agent,
    Harness,
    ModelConfig,
    Message,
    CallbackError,
    MessageStreamError,
    SessionConfiguration,
    ScriptedClient,
)
from agent_harness.agent_config import (
    atomic_write,
    default_profile,
    load_profile,
    toml_text,
)
from agent_harness.core.rollout import RolloutRecorder


def call(name, **arguments):
    return {
        "type": "function_call",
        "call_id": uuid4().hex,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


def say(text):
    return [call("send_user_message", text=text, wake_model=False)]


class SdkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.harness = Harness(self.root / "home")
        self.settings = SessionConfiguration(
            max_consecutive_steps=None, filesystem_policy=None
        )

    def create(self, script=(), **kwargs):
        return self.harness.create(
            self.work,
            client=ScriptedClient(list(script)),
            configuration=kwargs.pop("configuration", self.settings),
            **kwargs,
        )

    async def test_data_root_alias_preserves_thread_identity_after_relocation(self):
        import shutil
        old = self.root / "old"
        workspace = old / "workspace"
        workspace.mkdir(parents=True)
        harness = Harness(old / "runtime")
        (old / "control").mkdir()
        settings = SessionConfiguration(max_consecutive_steps=None,
            filesystem_policy={"rules": [{"path": str(old / "control"), "access": "deny"}]})
        agent = harness.create(workspace, client=ScriptedClient([]), configuration=settings)
        await agent.close()
        original = (agent.thread_path / "rollout.jsonl").read_bytes()
        new = self.root / "new"
        shutil.move(str(old), str(new))
        old.symlink_to(new, target_is_directory=True)
        profile = new / "runtime/threads" / agent.thread_id / "thread.toml"
        profile.write_text(profile.read_text().replace(str(old), str(new)))
        resumed = Harness(new / "runtime").resume(agent.thread_id, client=ScriptedClient([]))
        self.assertEqual(resumed.thread_id, agent.thread_id)
        self.assertEqual(resumed.workspace, new / "workspace")
        await resumed.close()
        self.assertTrue((resumed.thread_path / "rollout.jsonl").read_bytes().startswith(original))

    async def test_live_messages_callbacks_and_two_subscribers_have_no_turn_boundary(
        self,
    ):
        delivered = []

        async def receive(text):
            await asyncio.sleep(0.001)
            delivered.append(text)

        agent = self.create(
            [say("first"), say("second")], name="live", on_message=receive
        )
        self.assertIsInstance(agent, Agent)
        async with agent.messages() as one, agent.messages() as two:
            async with agent:
                await agent.send("hello")
                message = await asyncio.wait_for(anext(one), 2)
                self.assertIsInstance(message, Message)
                self.assertEqual(message.text, "first")
                self.assertEqual((await anext(two)).thread_id, agent.thread_id)
                self.assertFalse(agent.is_closed)
                await agent.send("continue")
                self.assertEqual((await asyncio.wait_for(anext(one), 2)).text, "second")
                self.assertEqual((await anext(two)).text, "second")
            with self.assertRaises(StopAsyncIteration):
                await anext(one)
        self.assertEqual(delivered, ["first", "second"])
        self.assertTrue(agent.is_closed)
        self.assertEqual(len(agent._streams), 0)
        self.assertFalse((agent.thread_path / "traces").exists())
        with self.assertRaises(RuntimeError):
            await agent.send("closed")
        await agent.close()

    async def test_pending_input_survives_pause_without_start_and_resume(self):
        first = self.create(name="pending")
        await first.send("queued before startup")
        await first.pause()
        second = self.harness.resume(
            "pending", client=ScriptedClient([say("restored")])
        )
        self.assertEqual(second.thread_id, first.thread_id)
        async with second.messages() as messages, second:
            self.assertEqual(
                (await asyncio.wait_for(anext(messages), 2)).text, "restored"
            )
        rows = [
            json.loads(line)
            for line in (second.thread_path / "rollout.jsonl").read_text().splitlines()
        ]
        inputs = [
            r
            for r in rows
            if r["type"] == "event_queued" and r["payload"]["kind"] == "user_message"
        ]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(len(self.harness.threads()), 1)

    async def test_template_overrides_and_saved_instruction_are_independent(self):
        path = self.harness.home / "agents" / "assistant" / "agent.toml"
        data = default_profile(self.work, self.harness.home)
        data["sandbox"]["enabled"] = False
        atomic_write(path.parent / "instructions.md", "template role")
        atomic_write(path, toml_text(data))
        self.assertEqual(self.harness.templates(), ["assistant"])
        agent = self.harness.from_template(
            "assistant",
            name="copy",
            overrides={"context.warning_tokens": 65000, "tools.code_mode": False},
            client=ScriptedClient([]),
        )
        self.assertEqual(
            load_profile(agent.config_path).configuration.context_budget_warning_tokens,
            65000,
        )
        self.assertEqual(
            load_profile(path).configuration.context_budget_warning_tokens, 100000
        )
        atomic_write(path.parent / "instructions.md", "different template")
        self.assertEqual(agent.instructions_path.read_text(), "template role")
        with self.assertRaisesRegex(ValueError, "already running"):
            self.harness.resume("copy", client=ScriptedClient([]))
        await agent.close()
        atomic_write(agent.instructions_path, "thread role")
        resumed = self.harness.resume("copy", client=ScriptedClient([]))
        async with resumed:
            self.assertEqual(
                resumed._session.agent_context.instructions(), "thread role"
            )

    async def test_config_factory_does_not_write_into_readonly_template_directory(self):
        folder = self.root / "readonly-template"
        data = default_profile(self.work, self.harness.home)
        data["sandbox"]["enabled"] = False
        atomic_write(folder / "instructions.md", "readonly source")
        atomic_write(folder / "agent.toml", toml_text(data))
        folder.chmod(0o555)
        try:
            agent = self.harness.from_config(
                folder / "agent.toml",
                client=ScriptedClient([]),
                overrides={"context.warning_tokens": 50000},
            )
            self.assertEqual(agent.instructions_path.read_text(), "readonly source")
            await agent.close()
            self.assertEqual(
                sorted(p.name for p in folder.iterdir()),
                ["agent.toml", "instructions.md"],
            )
        finally:
            folder.chmod(0o755)

    async def test_public_policy_update_receipt_and_same_live_handle(self):
        from unittest.mock import patch
        receipts=asyncio.Queue()
        def observe(event,data):
            queued=data.get("queued_event",{})
            if event=="event_enqueued" and queued.get("kind") in {"filesystem_policy_applied","filesystem_policy_failed"}:
                body=json.loads(queued["item"]["text"].removeprefix("<harness_event>").removesuffix("</harness_event>"))
                if body.get("source")=="runtime":receipts.put_nowait(body)
        agent=self.create([[]]*20,on_trace=observe)
        identity=(agent.thread_id,agent.session_id)
        async with agent:
            request=await agent.set_filesystem_policy(None)
            receipt=await asyncio.wait_for(receipts.get(),5)
            self.assertEqual(receipt["data"]["request_id"],request)
            self.assertEqual(receipt["kind"],"filesystem_policy_applied")
            self.assertEqual(receipt["data"]["policy_version"],2)
            self.assertEqual(identity,(agent.thread_id,agent.session_id))
            if Path('/usr/bin/bwrap').exists():
                with patch('agent_harness.core.sandbox.FilesystemSandbox.probe',side_effect=RuntimeError('candidate probe failed')):
                    request=await agent.set_filesystem_policy({"rules":[]})
                    receipt=await asyncio.wait_for(receipts.get(),5)
                    self.assertEqual(receipt["kind"],"filesystem_policy_failed")
                    self.assertEqual(receipt["data"]["request_id"],request)
                request=await agent.set_filesystem_policy(None)
                receipt=await asyncio.wait_for(receipts.get(),5)
                self.assertEqual(receipt["data"]["request_id"],request)
                self.assertEqual(receipt["data"]["policy_version"],3)
        with self.assertRaises(RuntimeError):await agent.set_filesystem_policy(None)

    async def test_policy_updates_coalesced_in_one_step_identify_superseded_requests(self):
        receipts=asyncio.Queue()
        def observe(event,data):
            queued=data.get("queued_event",{})
            if event=="event_enqueued" and queued.get("kind")=="filesystem_policy_applied":
                body=json.loads(queued["item"]["text"].removeprefix("<harness_event>").removesuffix("</harness_event>"))
                receipts.put_nowait(body["data"])
        agent=self.create([[]]*20,on_trace=observe)
        first=await agent.set_filesystem_policy(None)
        second=await agent.set_filesystem_policy(None)
        async with agent:
            receipt=await asyncio.wait_for(receipts.get(),5)
            self.assertEqual(receipt["request_id"],second)
            self.assertEqual(receipt["superseded_request_ids"],[first])

    async def test_live_mcp_resume_preserves_removal_and_respects_later_profile_edit(self):
        receipts = asyncio.Queue()
        def observe(event, data):
            if event == "mcp_configuration_applied": receipts.put_nowait(data)
        settings = SessionConfiguration(filesystem_policy=None,
            mcp_servers={"old": {"command": "unused", "enabled": False}})
        agent = self.create([[call("wait_for_event")]], configuration=settings, on_trace=observe)
        async with agent:
            request = await agent.set_mcp_servers({})
            self.assertEqual((await asyncio.wait_for(receipts.get(), 5))["request_id"], request)
        resumed = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        self.assertEqual(resumed._session.configuration.mcp_servers, {})
        async with resumed:
            pass
        profile = load_profile(agent.config_path)
        atomic_write(profile.resolve(profile.data["mcp"]["config"]),
            toml_text({"mcp_servers": {"edited": {"command": "unused", "enabled": False}}}))
        edited = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        self.assertEqual(set(edited._session.configuration.mcp_servers), {"edited"})
        await edited.close()

    async def test_live_mcp_addition_and_coalesced_receipts_survive_resume(self):
        receipts = asyncio.Queue()
        def observe(event, data):
            if event == "mcp_configuration_applied": receipts.put_nowait(data)
        agent = self.create([[call("wait_for_event")]], on_trace=observe)
        first = await agent.set_mcp_servers({"a": {"command": "unused", "enabled": False}})
        second = await agent.set_mcp_servers({"b": {"command": "unused", "enabled": False}})
        async with agent:
            receipt = await asyncio.wait_for(receipts.get(), 5)
            self.assertEqual(receipt["request_id"], second)
            self.assertEqual(receipt["superseded_request_ids"], [first])
        resumed = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        self.assertEqual(set(resumed._session.configuration.mcp_servers), {"b"})
        await resumed.close()

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp]")
    async def test_optional_mcp_failure_is_reported_without_exposing_config(self):
        receipts = asyncio.Queue()
        def observe(event, data):
            if event == "mcp_configuration_applied": receipts.put_nowait(data)
        agent = self.create([[call("wait_for_event")] for _ in range(3)], on_trace=observe)
        async with agent:
            request = await agent.set_mcp_servers({"optional": {
                "command": str(self.root / "missing-server"), "required": False}})
            receipt = await asyncio.wait_for(receipts.get(), 10)
            self.assertEqual(receipt['request_id'], request)
            self.assertEqual(receipt['unavailable'], ['optional'])
            self.assertEqual(agent.mcp_status, {'servers': [], 'unavailable': ['optional']})
            snapshot = agent.mcp_status
            snapshot['unavailable'].clear()
            self.assertEqual(agent.mcp_status['unavailable'], ['optional'])

    async def test_rejected_live_mcp_does_not_persist_candidate(self):
        from unittest.mock import AsyncMock, patch
        receipts = asyncio.Queue()
        def observe(event, data):
            if event == "mcp_configuration_rejected": receipts.put_nowait(data)
        agent = self.create([[call("wait_for_event")] for _ in range(3)], on_trace=observe)
        first = await agent.set_mcp_servers({"a": {"command": "unused", "enabled": False}})
        second = await agent.set_mcp_servers({"b": {"command": "unused", "enabled": False}})
        with patch.object(agent._session.services.mcp, 'replace_servers', AsyncMock(side_effect=ValueError('rejected'))):
            async with agent:
                receipt = await asyncio.wait_for(receipts.get(), 5)
                self.assertEqual(receipt['request_id'], second)
                self.assertEqual(receipt['superseded_request_ids'], [first])
        resumed = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        self.assertEqual(resumed._session.configuration.mcp_servers, {})
        await resumed.close()

    async def test_host_events_cannot_introduce_runtime_permission_metadata(self):
        complete = asyncio.Event()
        prompts = []

        def trace(event, data):
            if event == "model_request":
                prompts.append(data["prompt"])
            if event == "step_completed":
                complete.set()

        agent = self.create([[]], on_trace=trace)
        async with agent:
            event_id = await agent.emit(
                "file_changed", {"_filesystem_policy": {"rules": []}, "path": "a.txt"}
            )
            self.assertTrue(event_id)
            await asyncio.wait_for(complete.wait(), 2)
            await agent.flush_callbacks()
            self.assertTrue(
                any(
                    "file_changed" in item.get("text", "")
                    for item in prompts[0]["input"]
                )
            )
            self.assertIsNone(agent._session.configuration.filesystem_policy)
            with self.assertRaises(ValueError):
                await agent.emit("", {})
            with self.assertRaises(ValueError):
                await agent.emit("bad", float("nan"))

    async def test_wait_cancellation_does_not_cancel_agent_scheduler(self):
        agent = self.create([say("still here")])
        async with agent.messages() as messages, agent:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(agent.wait(), 0.02)
            await agent.send("hello")
            self.assertEqual(
                (await asyncio.wait_for(anext(messages), 2)).text, "still here"
            )

    async def test_application_callback_failure_is_observable_and_does_not_stop_model(
        self,
    ):
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: errors.append(context))
        self.addCleanup(loop.set_exception_handler, previous)

        async def broken(text):
            raise ValueError("application delivery failed")

        agent = self.create([say("one"), say("two")], on_message=broken)
        async with agent.messages() as messages:
            await agent.start()
            for text in ("one", "two"):
                await agent.send("next")
                self.assertEqual(
                    (await asyncio.wait_for(anext(messages), 2)).text, text
                )
            with self.assertRaises(CallbackError):
                await agent.close()
        self.assertEqual(len(agent.callback_errors), 2)
        self.assertEqual(len(errors), 2)
        self.assertTrue(agent.is_closed)
        resumed = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        await resumed.close()

    async def test_slow_message_subscriber_is_bounded_and_reports_overflow(self):
        agent = self.create([[*say("one"), *say("two")]])
        async with agent.messages(max_queue=1) as messages, agent:
            await agent.send("two messages")
            # Wait for both sends, rather than racing the first item out of its queue.
            async with asyncio.timeout(2):
                while not messages.closed:
                    await asyncio.sleep(0.005)
            self.assertEqual((await anext(messages)).text, "one")
            with self.assertRaises(MessageStreamError):
                await anext(messages)
            self.assertFalse(agent.is_closed)

    async def test_scheduler_failure_reaches_stream_and_releases_thread_lock(self):
        class Broken:
            def respond(self, prompt):
                raise RuntimeError("model transport failed")

        agent = self.harness.create(
            self.work, client=Broken(), configuration=self.settings
        )
        async with agent.messages() as messages:
            await agent.start()
            await agent.send("trigger")
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                await asyncio.wait_for(anext(messages), 2)
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                await agent.close()
        self.assertTrue(agent.is_closed)
        # A failed checkpoint requires explicit, audited recovery before resume.
        with self.assertRaises(ValueError):
            self.harness.resume(agent.thread_id, client=ScriptedClient([]))

    async def test_programmatic_settings_and_mcp_file_survive_staging_cleanup(self):
        settings = SessionConfiguration(
            base_instructions="custom base",
            max_consecutive_steps=42,
            mcp_servers={"disabled": {"command": "echo", "enabled": False}},
            filesystem_policy=None,
        )
        agent = self.create(
            name="configured", configuration=settings, instructions="custom role"
        )
        profile = load_profile(agent.config_path)
        self.assertTrue(profile.resolve(profile.data["mcp"]["config"]).is_file())
        self.assertNotIn(".sdk-profile-", profile.data["mcp"]["config"])
        await agent.close()
        resumed = self.harness.resume("configured", client=ScriptedClient([]))
        self.assertEqual(
            resumed._session.configuration.base_instructions, "custom base"
        )
        self.assertEqual(resumed._session.configuration.max_consecutive_steps, 42)
        self.assertEqual(resumed.instructions_path.read_text(), "custom role")
        await resumed.close()

    @unittest.skipUnless(Path("/usr/bin/bwrap").exists(), "Linux bubblewrap required")
    async def test_sdk_preserves_real_sandbox_and_external_write_permissions(self):
        outside = self.root / "outside.txt"
        script = [
            [call("exec_command", cmd="printf sdk > " + str(outside), login=False)],
            say("done"),
        ]
        settings = SessionConfiguration(
            max_consecutive_steps=None,
            filesystem_policy={"rules": [{"path": "/", "access": "write"}]},
        )
        agent = self.create(script, configuration=settings)
        async with agent.messages() as messages, agent:
            self.assertEqual((await self._send_receive(agent, messages)).text, "done")
        self.assertEqual(outside.read_text(), "sdk")
        saved = RolloutRecorder._read(
            (agent.thread_path / "rollout.jsonl").read_text(), agent.thread_id
        )
        self.assertIsNotNone(saved.meta["configuration"]["filesystem_policy"])

    async def _send_receive(self, agent, messages):
        await agent.send("work")
        return await asyncio.wait_for(anext(messages), 5)

    async def test_pause_waits_for_in_flight_step_and_keeps_final_output(self):
        from agent_harness import ModelResponse

        entered, release = threading.Event(), threading.Event()

        class Delayed:
            def respond(self, prompt):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test response release timed out")
                return ModelResponse(say("finished current step"))

        agent = self.harness.create(
            self.work, client=Delayed(), configuration=self.settings
        )
        async with agent.messages() as messages:
            await agent.start()
            await agent.send("work")
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            closing = asyncio.create_task(agent.pause())
            await asyncio.sleep(0.02)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 2)
            self.assertEqual((await anext(messages)).text, "finished current step")
            with self.assertRaises(StopAsyncIteration):
                await anext(messages)
        resumed = self.harness.resume(agent.thread_id, client=ScriptedClient([]))
        async with resumed:
            self.assertEqual(resumed.status, "waiting")

    async def test_concurrent_start_and_close_use_one_scheduler_and_one_footer(self):
        agent = self.create()
        results = await asyncio.gather(agent.start(), agent.start())
        self.assertEqual(results, [agent, agent])
        await asyncio.gather(agent.close(), agent.close())
        rows = [
            json.loads(line)
            for line in (agent.thread_path / "rollout.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(row["type"] == "session_closed" for row in rows), 1)

    async def test_callback_cannot_deadlock_by_closing_its_own_agent(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda *_: None)
        self.addCleanup(loop.set_exception_handler, previous)

        async def callback(text):
            await agent.close()

        agent = self.create([say("output")], on_message=callback)
        async with agent.messages() as messages:
            await agent.start()
            await agent.send("go")
            await asyncio.wait_for(anext(messages), 2)
            with self.assertRaises(CallbackError):
                await asyncio.wait_for(agent.close(), 2)
        self.assertIn("owner task", str(agent.callback_errors[0]))
        self.assertTrue(agent.is_closed)

    async def test_real_http_transport_trace_has_no_key_and_does_not_mutate_injected_client(
        self,
    ):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                data = json.dumps(
                    {"status": "completed", "output": say("http reply")}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            key = "sdk-dummy-key-not-real"
            env = self.root / "key.env"
            atomic_write(env, "SDK_TEST_KEY=" + key + "\n")
            model = ModelConfig(
                "test-model",
                f"http://127.0.0.1:{server.server_port}/responses",
                api_key_env="SDK_TEST_KEY",
                env_file=env,
                allow_http=True,
            )
            agent = self.harness.create(
                self.work,
                model=model,
                configuration=self.settings,
                trace=True,
                name="http",
            )
            async with agent.messages() as messages, agent:
                self.assertEqual(
                    (await self._send_receive(agent, messages)).text, "http reply"
                )
            self.assertEqual(requests[0]["model"], "test-model")
            self.assertIn("api_request", agent.trace_path.read_text())
            for path in agent.thread_path.rglob("*"):
                if path.is_file():
                    self.assertNotIn(key, path.read_text())
            resumed = self.harness.resume("http")
            async with resumed.messages() as messages, resumed:
                self.assertEqual(
                    (await self._send_receive(resumed, messages)).text, "http reply"
                )
            from agent_harness import ResponsesClient

            original = lambda *args: None
            client = ResponsesClient(
                model.url, model.name, key, allow_http=True, trace=original
            )
            other = self.harness.create(
                self.work, client=client, configuration=self.settings
            )
            self.assertIs(client.trace, original)
            await other.close()
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(2)

    def test_standalone_event_module_does_not_load_sdk_and_public_exports_are_available(
        self,
    ):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import agent_harness.events,sys;assert 'agent_harness.sdk' not in sys.modules;"
                "from agent_harness import Harness,Agent,ModelConfig,ModelResponse;assert 'agent_harness.management' not in sys.modules",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
