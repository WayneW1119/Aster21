"""A slow pause must not destroy a worker's final recovery checkpoint."""
import asyncio
import unittest
from unittest.mock import AsyncMock,Mock
from relay.store import Problem
import test_grants


class SafeStopTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_grants.GrantTests.setUp
    member=test_grants.GrantTests.member

    async def test_slow_pause_keeps_worker_owned_and_does_not_terminate(self):
        record=self.member()
        proc=Mock();proc.stdin.drain=AsyncMock()
        async def slow():await asyncio.sleep(60)
        proc.wait=AsyncMock(side_effect=slow)
        self.service.workers[record['id']]=proc
        with self.assertRaises(Problem):await self.service._stop(record['id'],timeout=.01)
        proc.terminate.assert_not_called();proc.kill.assert_not_called()
        self.assertIs(self.service.workers[record['id']],proc)
        self.assertEqual(self.service.store.agent(record['id'])['status'],'stopping')
        self.assertIsNone(self.service.store.agent(record['id'])['error'])

    async def test_shutdown_waits_for_checkpoint_without_interactive_deadline(self):
        record=self.member();self.service.workers[record['id']]=object()
        self.service._stop=AsyncMock()
        await self.service.close()
        self.service._stop.assert_awaited_once_with(record['id'],timeout=None)
        # close() already closed it; SQLite close is idempotent in cleanup.

    def test_wrapped_provider_quota_event_is_explained_without_leaking_body(self):
        from squad.worker import provider_failure
        message=provider_failure('api_stream_event',{'data':{'type':'error','error':{'code':'insufficient_quota','message':'secret upstream details'}}})
        self.assertIn('额度不足',message)
        self.assertNotIn('secret',message)

    async def test_space_stop_timeout_keeps_process_and_blocks_duplicate_start(self):
        from squad.fleet import Fleet
        fleet=object.__new__(Fleet)
        proc=Mock(returncode=None)
        async def slow():await asyncio.sleep(60)
        proc.wait=AsyncMock(side_effect=slow)
        fleet.processes={'space':proc};fleet.ready={'space'};fleet.stopping=set();fleet.locks={}
        fleet.record=Mock(return_value={'id':'space'})
        with self.assertRaises(Problem):await fleet.stop_process('space',timeout=.01)
        proc.kill.assert_not_called()
        self.assertIs(fleet.processes['space'],proc)
        self.assertIn('space',fleet.stopping);self.assertNotIn('space',fleet.ready)
        with self.assertRaises(Problem):await fleet.start('space')


    async def test_shutdown_quiesces_creation_before_waiting_for_worker_start(self):
        record=self.member();lock=self.service.lock(record['id'])
        await lock.acquire()
        self.service._stop=AsyncMock()
        closing=asyncio.create_task(self.service.close())
        await asyncio.sleep(.01)
        self.assertTrue(self.service.closing)
        self.assertFalse(closing.done())
        with self.assertRaises(Problem):await self.service.create('owner','late','Do not start')
        with self.assertRaises(Problem):await self.service._start(record['id'])
        lock.release();await closing
        self.service._stop.assert_awaited_once_with(record['id'],timeout=None)
