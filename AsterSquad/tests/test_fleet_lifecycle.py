"""Dynamic space allocation and safe removal without discarding agent data."""
import asyncio,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from relay.store import Problem
from squad.fleet import Fleet
from squad.service import Service


class FleetLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.args=SimpleNamespace(name='Default',mode='fake',port=19000,primary_port=19001,mcp_port=19002,relay_port=19003,relay_mcp_port=19004,models='',responses_url='https://example.invalid/responses',allow_http=False)
        self.fleet=Fleet(self.root,self.args);self.addCleanup(self.fleet.db.close)

    async def create(self,name):
        with patch.object(self.fleet,'start',AsyncMock()):return await self.fleet.create('squad',name)

    async def test_stopped_space_policy_persists_without_starting_it(self):
        await self.fleet.rename('studio', {'restore_members':True})
        self.assertTrue(self.fleet.public(self.fleet.record('studio'))['restore_members'])
        self.assertFalse(self.fleet.running('studio'))
        self.fleet.cache.clear()
        self.assertTrue((await self.fleet.detail('studio'))['resource']['restore_members'])
        await self.fleet.rename('studio', {'name':'Default', 'restore_members':False})
        self.assertFalse(self.fleet.public(self.fleet.record('studio'))['restore_members'])
        with self.assertRaises(Problem):
            await self.fleet.rename('studio', {'name':'Default','restore_members':'true'})

    async def test_more_than_eight_and_never_reuse_existing_data(self):
        old=self.root/'instances/slot-01';old.mkdir();(old/'keep.txt').write_text('preserve')
        for index in range(10):await self.create('Space '+str(index))
        self.assertEqual(len(self.fleet.records()),11)
        self.assertNotIn(str(old),[r['root'] for r in self.fleet.records()])
        self.assertEqual((old/'keep.txt').read_text(),'preserve')

    async def test_remove_retains_files_frees_name_and_allocates_fresh_directory(self):
        result=await self.create('Project');ident=result['resource']['id'];record=self.fleet.record(ident)
        folder=Path(record['root']);sentinel=folder/'keep.txt';sentinel.write_text('business data')
        removed=await self.fleet.remove(ident)
        self.assertTrue(removed['data_preserved'])
        with self.assertRaises(Problem):self.fleet.record(ident)
        self.assertEqual(sentinel.read_text(),'business data')
        archived=json.loads((self.fleet.private/'removed'/f'{ident}.json').read_text())
        self.assertEqual(archived['root'],str(folder));self.assertEqual(archived['enabled'],0)
        replacement=await self.create('Project')
        self.assertNotEqual(self.fleet.record(replacement['resource']['id'])['root'],str(folder))

    async def test_slow_stop_retains_registration_and_does_not_archive(self):
        result=await self.create('Slow');ident=result['resource']['id']
        with patch.object(self.fleet,'stop_process',AsyncMock(side_effect=Problem('Still stopping',409))):
            with self.assertRaises(Problem):await self.fleet.remove(ident)
        self.assertEqual(self.fleet.record(ident)['enabled'],0)
        self.assertFalse((self.fleet.private/'removed'/f'{ident}.json').exists())

    async def test_removing_last_space_does_not_bootstrap_it_again(self):
        await self.fleet.remove('studio');self.assertEqual(self.fleet.records(),[])
        self.fleet.db.close()
        restarted=Fleet(self.root,self.args);self.addCleanup(restarted.db.close)
        self.assertEqual(restarted.records(),[])
        with patch.object(restarted,'start',AsyncMock()):new=await restarted.create('squad','Fresh')
        self.assertEqual(new['resource']['name'],'Fresh')

    async def test_duplicate_name_does_not_leak_empty_partitions(self):
        await self.create('Unique');before=set((self.root/'instances').iterdir())
        with self.assertRaises(Problem):await self.create('unique')
        self.assertEqual(set((self.root/'instances').iterdir()),before)

    def test_future_peers_cannot_be_reopened_by_explicit_grants(self):
        for folder in [self.root,self.root/'instances/slot-01']:
            service=Service(folder,mode='fake',relay_url='http://127.0.0.1:1',relay_mcp='http://127.0.0.1:2/mcp',squad_mcp='http://127.0.0.1:3/mcp',owner_relay_token='test',domain='test',user_account='test',models=['fake'],model_url='https://example.invalid/responses',shared_skill_root=self.root)
            self.addCleanup(service.store.db.close)
            future=self.root/'instances/slot-99/workspaces/slot-00/new'
            with self.assertRaises(Problem):service.grants('owner',[{'path':str(future),'access':'write'}],workspace=service.slots[0])
            own=service.slots[0]/'new'
            self.assertEqual(service.grants('owner',[{'path':str(own),'access':'write'}],workspace=service.slots[0])[0]['path'],str(own))
            self.assertFalse(future.exists())
