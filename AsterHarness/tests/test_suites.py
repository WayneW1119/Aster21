import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_harness import Harness, SessionConfiguration, ScriptedClient


class SuiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        workspace = self.root / "work"
        workspace.mkdir()
        self.host = Harness(self.root / "host")
        self.agent = self.host.create(workspace, client=ScriptedClient([]),
            configuration=SessionConfiguration(filesystem_policy=None))
        self.addAsyncCleanup(self.agent.close)

    async def test_lifecycle_ownership_recovery_and_idle(self):
        folder = self.root / "demo"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: demo\ndescription: test skill\n---\nHello.")
        (folder / "suite.toml").write_text(
            'id = "demo"\nrequires = ["clock"]\nskills = [{path="."}]\n'
            '[generators.wake]\nkind="timer"\ndelay_ms=300000\nmessage="${message}"\n')
        agent = self.agent
        with self.assertRaisesRegex(ValueError, "Missing host permission"):
            await agent.install_suite(folder, parameters={"message": "ready"})
        await agent.configure_capabilities(grants=["clock"], allow_connect=True)
        await agent.install_suite(folder, parameters={"message": "ready"})
        with self.assertRaisesRegex(ValueError, "already installed"):
            await agent.install_suite(folder, parameters={"message": "duplicate"})
        await agent.start()
        session = agent._session
        entry, = session.generator_manager.list()
        self.assertEqual(entry["resource"], "demo/wake")
        self.assertEqual(entry["configuration"]["message"], "ready")
        with self.assertRaisesRegex(ValueError, "managed"):
            session.generator_manager.check_agent_control(entry["generator_id"])
        with self.assertRaisesRegex(ValueError, "managed"):
            session.skill_service.apply({"action": "unload", "skill_id": "demo"})
        self.assertEqual(session.state.step_number, 0)
        await agent.close()
        agent = self.host.resume(agent.thread_id, client=ScriptedClient([]))
        self.addAsyncCleanup(agent.close)
        await agent.start()
        self.assertEqual(agent._session.generator_manager.list()[0]["generator_id"], entry["generator_id"])
        await agent.set_suite_enabled("demo", False)
        self.assertEqual(agent._session.generator_manager.list(), [])
        self.assertNotIn("demo", agent._session.skill_service.scan()["entries"])
        await agent.set_suite_enabled("demo", True)
        replacement, = agent._session.generator_manager.list()
        self.assertEqual(replacement["resource"], entry["resource"])
        self.assertNotEqual(replacement["generator_id"], entry["generator_id"])
        await agent.remove_suite("demo")
        self.assertEqual(agent.list_suites(), {})
        self.assertEqual(agent._session.state.step_number, 0)


    async def test_completed_suite_timer_never_replays_on_resume(self):
        agent = self.agent
        await agent.install_suite({"id": "once", "generators": {"alarm": {
            "kind": "timer", "delay_ms": 1, "message": "one notification"}}})
        # Use a non-waking tool receipt for this scheduled event.
        from test_sdk import say
        agent._session.client = ScriptedClient([say("done")])
        await agent.start()
        async with asyncio.timeout(3):
            while not agent._session.state.sent_messages:
                await asyncio.sleep(.01)
        await agent.close()
        agent = self.host.resume(agent.thread_id, client=ScriptedClient([]))
        self.addAsyncCleanup(agent.close)
        await agent.start()
        source, = agent._session.generator_manager.list(True)
        self.assertEqual((source["status"], source["events_emitted"]), ("completed", 1))
        self.assertEqual(agent._session.state.step_number, 1)
