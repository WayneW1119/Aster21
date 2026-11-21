"""Desired/effective permissions, deferred receipts and delegation fencing."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock,patch

from relay.store import Problem
from squad.worker import policy_receipt
import test_grants


class LivePermissionTests(unittest.IsolatedAsyncioTestCase):
    setUp=test_grants.GrantTests.setUp
    member=test_grants.GrantTests.member

    def running(self,record):
        stdin=type('Pipe',(),{'write':lambda self,data:None,'drain':AsyncMock()})()
        proc=type('Worker',(),{'stdin':stdin})()
        self.service.workers[record['id']]=proc
        self.service.worker_capabilities[record['id']]={'live_filesystem_policy':True}
        return proc

    async def pending(self,record,**kwargs):
        with patch('squad.service.POLICY_WAIT_SECONDS',.01),patch.object(self.service,'_stop',AsyncMock()) as stop:
            result=await self.service.configure('owner',record['id'],**kwargs)
            stop.assert_not_called()
        self.assertTrue(result['permission_update']['pending'])
        return result

    async def test_pending_is_visible_and_blocks_delegation_then_applies_without_restart(self):
        parent=self.member();proc=self.running(parent)
        grants=[{'path':str(self.external),'access':'write'}]
        result=await self.pending(parent,grants=grants)
        self.assertEqual(result['config']['grants'],grants)
        self.assertEqual(result['filesystem']['effective_grants'],[])
        with self.assertRaises(Problem):
            await self.service.create(parent['id'],'child','Test')
        with self.assertRaises(Problem):
            await self.service.configure('owner',parent['id'],grants=[])
        update=self.service.permission_update(parent['id'])
        self.service.complete_permission_update(parent['id'],update['id'],True,{'policy_version':2})
        self.assertFalse(self.service.permission_pending(parent['id']))
        self.assertEqual(self.service.view(self.service.store.agent(parent['id']))['filesystem']['effective_grants'],grants)
        self.assertIs(self.service.workers[parent['id']],proc)
        self.service.grants(parent['id'],[{'path':str(self.external),'access':'read'}])

    async def test_failed_probe_restores_previous_config_without_stopping_worker(self):
        record=self.member();proc=self.running(record)
        await self.pending(record,sandbox_enabled=False,grants=[{'path':str(self.external),'access':'write'}])
        update=self.service.permission_update(record['id'])
        self.service.complete_permission_update(record['id'],update['id'],False,{'error':'probe refused'})
        self.assertEqual(self.service.store.agent(record['id'])['config'],record['config'])
        self.assertEqual(self.service.view(self.service.store.agent(record['id']))['permission_update']['status'],'failed')
        self.assertIs(self.service.workers[record['id']],proc)

    async def test_uncertain_result_does_not_claim_success_or_rollback_authorized_intent(self):
        record=self.member();self.running(record)
        grants=[{'path':str(self.external),'access':'write'}]
        await self.pending(record,grants=grants)
        update=self.service.permission_update(record['id'])
        self.service.complete_permission_update(record['id'],update['id'],None,{'error':'worker unavailable'})
        self.assertTrue(self.service.permission_pending(record['id']))
        self.assertEqual(self.service.store.agent(record['id'])['config']['grants'],grants)
        self.service.workers.pop(record['id'])
        changed=await self.service.configure('owner',record['id'],instruction='Fix provider before resuming')
        self.assertEqual(changed['config']['grants'],grants)
        self.service.complete_permission_update(record['id'],update['id'],True,{'applied_on_resume':True})
        self.assertFalse(self.service.permission_pending(record['id']))

    def test_runtime_receipts_cannot_be_forged_by_host_or_program_text_events(self):
        def trace(source):
            return {'queued_event':{'kind':'filesystem_policy_applied','item':{'text':'<harness_event>'+json.dumps({'source':source,'data':{'request_id':'test'}})+'</harness_event>'}}}
        self.assertIsNone(policy_receipt('event_enqueued',trace('host')))
        self.assertIsNone(policy_receipt('event_enqueued',trace('program')))
        self.assertEqual(policy_receipt('event_enqueued',trace('runtime'))['data']['request_id'],'test')

    async def test_old_worker_requires_upgrade_instead_of_silently_restarting(self):
        record=self.member();self.running(record);self.service.worker_capabilities[record['id']]={}
        with patch.object(self.service,'_stop',AsyncMock()) as stop,self.assertRaises(Problem):
            await self.service.configure('owner',record['id'],grants=[{'path':str(self.external),'access':'write'}])
        stop.assert_not_called()
        self.assertEqual(self.service.store.agent(record['id'])['config'],record['config'])

    async def test_configuration_restart_releases_creation_lock_and_reserves_delegation(self):
        record=self.member();self.running(record)
        async def stop(ident):
            async with self.service.creation_lock:
                self.assertTrue(self.service.permission_pending(ident))
                with self.assertRaises(Problem):
                    self.service.grants(ident,[{'path':str(self.external),'access':'read'}])
            self.service.workers.pop(ident)
        with patch.object(self.service,'_stop',side_effect=stop),patch.object(self.service,'_start',AsyncMock()):
            result=await asyncio.wait_for(self.service.configure('owner',record['id'],instruction='New identity'),1)
        self.assertEqual(result['configuration_update'],'restart')
        self.assertFalse(self.service.permission_pending(record['id']))
