import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from relay.server import Service, make_app
from relay.store import Problem, Store


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "domain.db", "Test domain")
        owner = self.store.create(None, "user", "User")
        self.owner, self.token = owner["account"]["id"], owner["token"]
        self.alice = self.store.create(self.owner, "alice")
        self.bob = self.store.create(self.owner, "bob")
        self.service = Service(self.store)
        self.client = TestClient(TestServer(make_app(self.service)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.db.close()
        self.temp.cleanup()

    async def test_host_publishes_agent_endpoint_without_restarting_or_changing_accounts(self):
        from relay.server import local_agent_url
        before=self.store.domain()
        self.store.publish_agent_url('http://127.0.0.1:42161/')
        response=await self.client.get('/api/state',headers={'Authorization':'Bearer '+self.token})
        state=await response.json()
        self.assertEqual(state['domain']['agent_url'],'http://127.0.0.1:42161')
        self.assertEqual(state['domain']['id'],before['id'])
        self.assertEqual(state['me']['id'],self.owner)
        self.assertEqual((await self.client.get('/api/state')).status,401)
        self.store.publish_agent_url('http://127.0.0.1:42162')
        self.assertEqual(self.store.domain()['agent_url'],'http://127.0.0.1:42162')
        for invalid in ('file:///tmp/x','http://user:secret@localhost:123','https://example.com/?token=secret'):
            with self.assertRaises(ValueError):self.store.publish_agent_url(invalid)
        self.assertEqual(local_agent_url('0.0.0.0',123),'http://127.0.0.1:123')
        self.assertEqual(local_agent_url('::',123),'http://[::1]:123')

    async def test_standalone_install_manifest_and_assets(self):
        response = await self.client.get('/manifest.webmanifest')
        self.assertEqual(response.status, 200)
        import json
        manifest = json.loads(await response.text())
        self.assertEqual(manifest['start_url'], './')
        self.assertEqual(manifest['display'], 'standalone')
        for path in ('icon-192.png', 'icon-512.png', 'app-shell.js', 'sw.js'):
            self.assertEqual((await self.client.get('/'+path)).status, 200)
        self.assertEqual((await self.client.get('/../server.py')).status, 404)

    async def test_authenticated_sender_and_conversation_privacy(self):
        response = await self.client.get("/api/state")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/api/messages", headers={"Authorization": "Bearer " + self.alice["token"]},
            json={"to": self.bob["account"]["id"], "text": "hello", "sender": self.owner, "from": "human"})
        row = await response.json()
        self.assertEqual(row["sender"], self.alice["account"]["id"])
        self.assertEqual(self.store.history(self.owner, self.bob["account"]["id"]), [])

    async def test_queue_ack_read_idempotency_and_persistence(self):
        aid, bid = self.alice["account"]["id"], self.bob["account"]["id"]
        first = self.store.send(aid, bid, "hello", "retry-1")
        second = self.store.send(aid, bid, "hello", "retry-1")
        self.assertEqual(first["id"], second["id"])
        with self.assertRaises(Problem):
            self.store.send(aid, bid, "different", "retry-1")
        self.assertEqual(self.store.inbox(bid), self.store.inbox(bid))
        self.store.acknowledge(aid, [first["id"]], True)
        self.assertEqual(len(self.store.inbox(bid)), 1)
        self.store.acknowledge(bid, [first["id"]])
        self.assertEqual(self.store.inbox(bid), [])
        self.assertEqual(self.store.history(aid, bid)[0]["read"], 0)
        self.store.acknowledge(bid, [first["id"]], True)
        reopened = Store(Path(self.temp.name) / "domain.db")
        try:
            self.assertEqual(reopened.history(aid, bid)[0]["read"], 1)
            self.assertEqual(reopened.domain()["id"], self.store.domain()["id"])
        finally:
            reopened.db.close()

    async def test_account_management_and_revocation(self):
        aid, bid = self.alice["account"]["id"], self.bob["account"]["id"]
        kid = self.store.create(aid, "kid")
        self.assertTrue(self.store.manages(self.owner, kid["account"]["id"]))
        with self.assertRaises(Problem):
            self.store.manage(aid, bid, "disable")
        with self.assertRaises(Problem):
            self.store.manage(aid, aid, "disable")
        result = self.store.manage(self.owner, aid, "reset_token")
        with self.assertRaises(Problem):
            self.store.authenticate(self.alice["token"])
        self.assertEqual(self.store.authenticate(result["token"])["id"], aid)
        self.store.manage(self.owner, aid, "disable")
        with self.assertRaises(Problem):
            self.store.authenticate(kid["token"])
        self.store.manage(self.owner, aid, "enable")
        self.assertEqual(self.store.authenticate(kid["token"])["id"], kid["account"]["id"])
        plain = self.store.create(self.owner, "plain", can_create=False)
        with self.assertRaises(Problem):
            self.store.create(plain["account"]["id"], "unauthorized")

    async def test_long_poll_wakes_and_does_not_ack(self):
        bid = self.bob["account"]["id"]
        waiter = asyncio.create_task(self.service.wait(bid, 0, 2))
        await asyncio.sleep(.03)
        message = self.store.send(self.owner, bid, "wake up")
        self.service.changed.set()
        response = await asyncio.wait_for(waiter, 1)
        self.assertEqual(response["latest"], message["id"])
        self.assertEqual(response["pending"], 1)
        self.assertEqual(len(self.store.inbox(bid)), 1)

    async def test_domain_unique_usernames_and_username_addressing(self):
        aid, bid = self.alice["account"]["id"], self.bob["account"]["id"]
        with self.assertRaises(Problem) as conflict:
            self.store.create(self.owner, "@ALICE", "different display name")
        self.assertEqual(conflict.exception.status, 409)
        self.store.create(self.owner, "charlie", "Same name")
        self.store.create(self.owner, "dana", "Same name")
        self.assertEqual(self.store.resolve("@ALICE"), aid)
        message = self.store.send(aid, "@BOB", "by username")
        self.assertEqual(message["recipient"], bid)
        public = self.store.public_message(message)
        self.assertEqual(public["sender"], "alice")
        self.assertEqual(public["recipient"], "bob")
        self.assertEqual(self.store.history(aid, "bob")[0]["id"], message["id"])
        self.store.manage(self.owner, "@bob", "disable")
        with self.assertRaises(Problem):
            self.store.send(aid, "bob", "disabled")
        self.store.manage(self.owner, "bob", "enable")
        response = await self.client.post("/api/accounts", headers={"Authorization": "Bearer " + self.token},
            json={"username": "new-user", "name": "New user"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["account"]["username"], "new-user")


if __name__ == "__main__":
    unittest.main()
