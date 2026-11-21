"""Historical runtime errors must not prevent editing an offline member."""
import unittest
from unittest.mock import AsyncMock,patch

from relay.store import Problem
import test_grants


class ConfigurationErrorTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_grants.GrantTests.setUp
    member=test_grants.GrantTests.member

    async def test_offline_error_can_save_paths_without_clearing_error_or_starting(self):
        record=self.member()
        self.service.store.update(record['id'],status='error',error='Could not reach model endpoint')
        target=self.external/'new-project'
        with patch.object(self.service,'_start',AsyncMock()) as start:
            result=await self.service.configure('owner',record['id'],grants=[{'path':str(target),'access':'write'}])
            start.assert_not_called()
        self.assertEqual(result['error'],'Could not reach model endpoint')
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['thread'],record['thread'])
        self.assertEqual(result['config']['grants'],[{'path':str(target),'access':'write'}])
        self.assertTrue(target.is_dir())

    async def test_failed_stop_still_blocks_configuration_change(self):
        record=self.member()
        self.service.workers[record['id']]=object()
        async def failed_stop(ident):
            self.service.store.update(ident,status='error',error='Pause timed out')
        with patch.object(self.service,'_stop',failed_stop),patch.object(self.service,'_start',AsyncMock()) as start:
            with self.assertRaises(Problem):
                await self.service.configure('owner',record['id'],instruction='Changed')
            start.assert_not_called()
        self.assertEqual(self.service.store.agent(record['id'])['config'],record['config'])

    async def test_clean_stop_applies_and_restarts_existing_member(self):
        record=self.member()
        self.service.workers[record['id']]=object()
        with patch.object(self.service,'_stop',AsyncMock()) as stop,patch.object(self.service,'_start',AsyncMock()) as start:
            result=await self.service.configure('owner',record['id'],instruction='Changed')
            stop.assert_awaited_once_with(record['id'])
            start.assert_awaited_once_with(record['id'])
        self.assertEqual(result['config']['instruction'],'Changed')
