import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_harness.core.client import ScriptedClient
from agent_harness.core.event_generators.clock import AgentClock
from agent_harness.core.event_generators import EventGeneratorManager
from agent_harness.core.event_generators.timer import TimerGenerator
from agent_harness.core.thread_manager import ThreadManager


class AgentClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_offset_and_deadline_survive_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            threads = ThreadManager(root / "threads")
            agent = threads.create(ScriptedClient([]), root)

            async def tool(name, **args):
                return json.loads(await agent.router.registry.execute_async(name, args))

            try:
                self.assertEqual((await tool("agent_time"))["utc_offset"], "+00:00")
                await tool("agent_time", utc_offset="+8")
                year = datetime.now(timezone.utc).year + 1
                timer = await tool("create_timer", at=f"{year}-01-01 09:00", message="wake")
                self.assertEqual(timer["next_fire_at"], f"{year}-01-01T09:00:00+08:00")
                await tool("agent_time", utc_offset="-05:30")
                await agent.suspend()
                tid = agent.thread_id
                await agent.close()
                agent = threads.resume(ScriptedClient([]), tid)
                self.assertEqual((await tool("agent_time"))["utc_offset"], "-05:30")
                deadline = (await tool("list_event_generators"))[0]["next_fire_at"]
                self.assertEqual(deadline, f"{year-1}-12-31T19:30:00-05:30")
                explicit = await tool("create_timer", at=f"{year}-01-01T01:00Z", message="same")
                self.assertEqual(explicit["next_fire_at"], deadline)
            finally:
                await agent.close()

    async def test_event_uses_current_offset_at_emission(self):
        events = asyncio.Queue()
        manager = EventGeneratorManager(events.put, lambda *_: None, lambda: asyncio.sleep(0))
        try:
            manager.register(TimerGenerator(1, "wake"))
            manager.clock.set("+5:45")
            event = await asyncio.wait_for(events.get(), 1)
            self.assertTrue(event["occurred_at"].endswith("+05:45"))
            self.assertEqual(event["data"], {"message": "wake", "occurrence": 1})
        finally:
            await manager.close()

    def test_invalid_offset_or_timestamp(self):
        clock = AgentClock()
        for value in ("+24", "-1:60", "+foo", 8):
            with self.assertRaises(ValueError):
                clock.set(value)
        for value in ("09:00", "2026-02-30 09:00", "2027-01-01T09:00+05:99"):
            with self.assertRaises(ValueError):
                clock.deadline(value)
