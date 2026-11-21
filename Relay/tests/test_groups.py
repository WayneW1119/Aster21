"""Standalone group lifecycle, moderation, queue compatibility and migration."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from aiohttp.test_utils import TestClient,TestServer
from relay.store import Store,Problem,digest
from relay.server import Service,make_app


class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'relay.db'
        self.store=Store(self.path);self.owner=self.store.create(None,'user')
        self.alice=self.store.create(self.owner['account']['id'],'alice',can_create=False)
        self.bob=self.store.create(self.owner['account']['id'],'bob')
        self.service=Service(self.store);self.client=TestClient(TestServer(make_app(self.service)));await self.client.start_server()
        self.oid=self.owner['account']['id'];self.aid=self.alice['account']['id'];self.bid=self.bob['account']['id']

    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.temp.cleanup()

    def group(self):return self.store.create_group(self.oid,'项目讨论',['alice','bob'])['id']
    def header(self,who):return {'Authorization':'Bearer '+who['token']}

    async def test_broadcast_independent_ack_and_private_message_isolation(self):
        gid=self.group();sent=self.store.send_group(self.oid,gid,'群消息','once')
        a=self.store.inbox(self.aid);b=self.store.inbox(self.bid)
        self.assertEqual(len(a),1);self.assertEqual(len(b),1);self.assertNotEqual(a[0]['id'],b[0]['id'])
        self.assertEqual(a[0]['group_message_id'],sent['id']);self.assertEqual(self.store.inbox(self.oid),[])
        self.assertEqual(self.store.history(self.oid,'alice'),[])
        self.assertEqual(self.store.send_group(self.oid,gid,'群消息','once')['id'],sent['id'])
        self.assertEqual(len(self.store.inbox(self.aid)),1)
        with self.assertRaises(Problem):self.store.send_group(self.oid,gid,'不同内容','once')
        self.store.acknowledge(self.aid,[a[0]['id']],True)
        self.assertEqual(self.store.inbox(self.aid),[]);self.assertEqual(len(self.store.inbox(self.bid)),1)
        response=await self.client.get('/api/state',headers=self.header(self.bob));state=await response.json()
        self.assertEqual(state['unread'],{});self.assertEqual(state['groups'][0]['unread'],1)
        public=self.store.public_message(b[0]);self.assertEqual(public['kind'],'group');self.assertEqual(public['group_id'],gid);self.assertEqual(public['sender'],'user')

    async def test_membership_and_group_authority_do_not_follow_account_management(self):
        g=self.store.create_group(self.aid,'Alice 的群',['bob']);gid=g['id']
        self.assertTrue(self.store.manages(self.oid,self.aid))
        with self.assertRaises(Problem):self.store.group(self.oid,gid)
        for actor in (self.oid,self.bid):
            with self.assertRaises(Problem):self.store.manage_group(actor,gid,'mute_all')
        self.store.manage_group(self.aid,gid,'rename',name='新的群名')
        self.assertEqual(self.store.group(self.bid,gid)['name'],'新的群名')
        with self.assertRaises(Problem):self.store.manage_group(self.aid,gid,'leave')
        for action in ('remove','mute'):
            with self.assertRaises(Problem):self.store.manage_group(self.aid,gid,action,member='alice')

    async def test_individual_and_global_mutes_enforced_by_server(self):
        gid=self.group();self.store.manage_group(self.oid,gid,'mute',member='alice')
        with self.assertRaises(Problem):self.store.send_group(self.aid,gid,'blocked')
        self.store.send_group(self.bid,gid,'allowed')
        self.store.manage_group(self.oid,gid,'mute_all')
        with self.assertRaises(Problem):self.store.send_group(self.bid,gid,'also blocked')
        self.store.send_group(self.oid,gid,'owner notice')
        self.store.manage_group(self.oid,gid,'unmute_all')
        with self.assertRaises(Problem):self.store.send_group(self.aid,gid,'still individually muted')
        self.store.manage_group(self.oid,gid,'unmute',member='alice');self.store.send_group(self.aid,gid,'unmuted')
        response=await self.client.post(f'/api/groups/{gid}',headers=self.header(self.bob),json={'action':'mute_all'})
        self.assertEqual(response.status,403)

    async def test_removal_rejoin_and_leave_revoke_queue_and_history_access(self):
        gid=self.group();self.store.send_group(self.oid,gid,'before removal')
        self.store.manage_group(self.oid,gid,'remove',member='alice')
        self.assertEqual(self.store.inbox(self.aid),[]);self.assertEqual(self.store.groups(self.aid),[])
        with self.assertRaises(Problem):self.store.group_history(self.aid,gid)
        self.store.send_group(self.oid,gid,'while absent')
        self.store.manage_group(self.oid,gid,'invite',members=['alice'])
        self.assertEqual(self.store.group_history(self.aid,gid),[]);self.assertEqual(self.store.inbox(self.aid),[])
        self.store.send_group(self.oid,gid,'after rejoin')
        self.assertEqual([m['text'] for m in self.store.group_history(self.aid,gid)],['after rejoin'])
        self.assertEqual(len(self.store.inbox(self.aid)),1)
        self.store.manage_group(self.bid,gid,'leave')
        with self.assertRaises(Problem):self.store.send_group(self.bid,gid,'outsider')
        self.store.manage_group(self.oid,gid,'dissolve')
        self.assertEqual(self.store.groups(self.oid),[]);self.assertEqual(self.store.inbox(self.aid),[])

    async def test_new_members_only_see_new_history_and_pagination_persists(self):
        gid=self.store.create_group(self.oid,'History')['id'];self.store.send_group(self.oid,gid,'old')
        self.store.manage_group(self.oid,gid,'invite',members=['alice'])
        for text in ('one','two','three'):self.store.send_group(self.oid,gid,text)
        page=self.store.group_history(self.aid,gid,limit=2)
        self.assertEqual([r['text'] for r in page],['two','three'])
        self.assertEqual([r['text'] for r in self.store.group_history(self.aid,gid,before=page[0]['id'],limit=2)],['one'])
        reopened=Store(self.path)
        try:self.assertEqual(len(reopened.group_history(self.aid,gid)),3)
        finally:reopened.db.close()

    async def test_group_delivery_wakes_existing_watcher_and_http_authenticates_sender(self):
        gid=self.group();wait=asyncio.create_task(self.service.wait(self.aid,0,2));await asyncio.sleep(.02)
        response=await self.client.post(f'/api/groups/{gid}/messages',headers=self.header(self.bob),json={'text':'wake','sender':self.oid})
        self.assertEqual(response.status,200);self.assertEqual((await response.json())['sender'],self.bid)
        result=await asyncio.wait_for(wait,1);self.assertEqual(result['pending'],1)
        self.assertEqual(len(self.store.inbox(self.aid)),1)
        response=await self.client.post('/api/groups',headers=self.header(self.alice),json={'name':'Plain account can create','members':['user']})
        self.assertEqual(response.status,200);self.assertEqual((await response.json())['owner'],self.aid)
        self.assertEqual((await self.client.get('/api/groups')).status,401)

    async def test_invalid_invites_and_idempotency_fail_without_partial_fanout(self):
        gid=self.group();before=len(self.store.group(self.oid,gid)['members'])
        with self.assertRaises(Problem):self.store.manage_group(self.oid,gid,'invite',members=['alice','missing'])
        self.assertEqual(len(self.store.group(self.oid,gid)['members']),before)
        self.store.send_group(self.oid,gid,'same','retry')
        other=self.store.create_group(self.oid,'Other',['alice'])['id']
        with self.assertRaises(Problem):self.store.send_group(self.oid,other,'same','retry')
        self.assertEqual(self.store.group_history(self.aid,other),[])
        for invalid in (None,True,0,2**64):
            with self.subTest(invalid=invalid),self.assertRaises(Problem):self.store.group(self.oid,invalid)

    async def test_legacy_database_migrates_without_losing_direct_messages(self):
        path=Path(self.temp.name)/'legacy.db'
        from contextlib import closing
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''CREATE TABLE domain(id TEXT PRIMARY KEY,name TEXT NOT NULL);
            CREATE TABLE accounts(id TEXT PRIMARY KEY,handle TEXT UNIQUE COLLATE NOCASE NOT NULL,name TEXT NOT NULL,manager TEXT,can_create INTEGER NOT NULL,disabled INTEGER NOT NULL DEFAULT 0,token_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,sender TEXT NOT NULL,recipient TEXT NOT NULL,text TEXT NOT NULL,created REAL NOT NULL,client_id TEXT,acked INTEGER NOT NULL DEFAULT 0,read INTEGER NOT NULL DEFAULT 0,UNIQUE(sender,client_id));''')
            db.execute('INSERT INTO domain VALUES(?,?)',('domain','Legacy'))
            for ident in ('a','b'):db.execute('INSERT INTO accounts VALUES(?,?,?,?,?,?,?,?)',(ident,ident,ident,None,1,0,digest(ident),1))
            db.execute('INSERT INTO messages(sender,recipient,text,created) VALUES(?,?,?,?)',('a','b','existing message',1))
            db.commit()
        migrated=Store(path)
        try:
            self.assertEqual(migrated.domain()['id'],'domain');self.assertEqual(migrated.inbox('b')[0]['text'],'existing message')
            group=migrated.create_group('a','Migrated',['b']);migrated.send_group('a',group['id'],'new group message')
            self.assertEqual(len(migrated.inbox('b')),2)
            self.assertEqual(len(migrated.history('a','b')),1)
        finally:migrated.db.close()
