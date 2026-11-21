"""Same-origin phone routes: separate app authorities and scoped Relay cookies, no MCP."""
import argparse
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from relay.server import Service as RelayService, make_app as relay_app, secret_file
from relay.store import Store
from squad.fleet import Fleet, make_app


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "relay.db")
        owner = self.store.create(None, "user")
        self.relay_token = owner["token"]
        self.owner = owner["account"]["id"]
        self.person = self.store.create(self.owner, "person")
        self.observed = []

        @web.middleware
        async def capture(request, handler):
            self.observed.append(dict(request.headers))
            return await handler(request)

        upstream_app = relay_app(RelayService(self.store,"https://mobile.example.test:8443/relay/domains/studio"))
        upstream_app.middlewares.insert(0, capture)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        args = argparse.Namespace(port=0, primary_port=1, mcp_port=2, relay_port=self.upstream.port,
            relay_mcp_port=4, name="Phone test", mode="fake", models="", public_url="https://mobile.example.test:8443")
        self.fleet = Fleet(self.root / "fleet", args)
        self.fleet.processes["studio"] = SimpleNamespace(returncode=None)
        self.fleet.ready.add("studio")
        self.fleet.cache["studio"] = {"agents":[],"relay_url":self.fleet.relay_url(self.fleet.record("studio"))}
        secret_file(self.fleet.token_file(self.fleet.record("studio"), relay=True), self.relay_token)
        self.manager_token = "isolated-manager-token"
        self.client = TestClient(TestServer(make_app(self.fleet, self.manager_token)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.store.db.close()
        self.fleet.db.close()
        self.temp.cleanup()

    def header(self, token):
        return {"Authorization":"Bearer "+token}

    async def test_prefixed_assets_and_configured_external_links(self):
        for path in ["/squad/","/squad/console.js","/squad/console.css","/relay/","/relay/app.js","/relay/style.css","/relay/domains/studio/"]:
            response = await self.client.get(path)
            self.assertEqual(response.status,200,path)
        response = await self.client.get("/relay/")
        body = await response.text()
        self.assertIn('content="https://mobile.example.test:8443/relay/domains/studio"',body)
        self.assertIn('src="/relay/app.js"',body)
        response = await self.client.get("/squad/api/relay-connections",headers={**self.header(self.manager_token),"Host":"untrusted.example"})
        self.assertEqual(response.status,200)
        data = await response.json()
        bundle = json.loads(unquote(urlsplit(data["url"]).fragment.removeprefix("connections=")))
        self.assertTrue(data["url"].startswith("https://mobile.example.test:8443/relay/"))
        self.assertEqual(bundle["manager"],"https://mobile.example.test:8443/squad")
        self.assertTrue(all(c["url"].startswith("https://mobile.example.test:8443/relay/domains/") for c in bundle["connections"]))
        self.assertNotIn("127.0.0.1",data["url"])
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    async def test_manager_and_domain_credentials_remain_separate(self):
        for token in ["",self.relay_token]:
            response = await self.client.get("/squad/api/relay-connections",headers=self.header(token))
            self.assertEqual(response.status,401)
            self.assertEqual(response.headers["Cache-Control"],"no-store")
        for token in ["",self.manager_token]:
            response = await self.client.get("/relay/domains/studio/api/state",headers=self.header(token))
            self.assertEqual(response.status,401)
        response = await self.client.get("/relay/domains/studio/api/state",headers=self.header(self.relay_token))
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())["me"]["username"],"user")
        for path in ["/relay/domains/studio/mcp","/relay/domains/studio/api/mcp","/relay/domains/studio/api/relay-connections"]:
            response = await self.client.get(path,headers=self.header(self.relay_token))
            self.assertEqual(response.status,404)

    async def test_group_routes_require_relay_identity_and_preserve_membership(self):
        base='/relay/domains/studio/api'
        response=await self.client.post(base+'/groups',headers=self.header(self.relay_token),json={'name':'Group','members':['person']})
        self.assertEqual(response.status,200)
        group=await response.json()
        response=await self.client.get(base+'/groups/'+str(group['id']),headers=self.header(self.person['token']))
        self.assertEqual(response.status,200)
        response=await self.client.post(base+f'/groups/{group["id"]}/messages',headers=self.header(self.person['token']),json={'text':'hello'})
        self.assertEqual(response.status,200)
        response=await self.client.get(base+f'/groups/{group["id"]}/messages',headers=self.header(self.relay_token))
        self.assertEqual((await response.json())['messages'][0]['text'],'hello')
        response=await self.client.post(base+f'/groups/{group["id"]}',headers=self.header(self.person['token']),json={'action':'mute_all'})
        self.assertEqual(response.status,403)
        response=await self.client.get(base+'/groups',headers=self.header(self.manager_token))
        self.assertEqual(response.status,401)

    async def test_browser_session_only_authenticates_squad_and_requires_origin_on_writes(self):
        from squad.browser_auth import COOKIE
        response=await self.client.post('/squad/auth/password/set',headers={
            **self.header(self.manager_token),'Origin':'https://mobile.example.test:8443'},
            json={'username':'owner','password':'test-a-long-password'})
        self.assertEqual(response.status,200)
        cookie={'Cookie':COOKIE+'='+response.cookies[COOKIE].value}
        # This fixture has a Relay upstream only; Squad state comes from its cached snapshot.
        self.fleet.ready.discard('studio')
        self.fleet.processes.pop('studio')
        response=await self.client.get('/squad/api/state',headers=cookie)
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        for path in ['/api/state','/relay/domains/studio/api/state']:
            self.assertEqual((await self.client.get(path,headers=cookie)).status,401)
        response=await self.client.post('/squad/api/domains',headers=cookie,json={'name':'must fail'})
        self.assertEqual(response.status,403)
        response=await self.client.post('/squad/auth/logout',headers={**cookie,'Origin':'https://mobile.example.test:8443'},json={})
        self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get('/squad/api/state',headers=cookie)).status,401)

    async def test_independent_relay_browser_session_and_cookie_proxy(self):
        origin='https://mobile.example.test:8443'
        response=await self.client.post('/relay/domains/studio/api/auth/password/set',
            headers={**self.header(self.relay_token),'Origin':origin},json={'password':'relay-own-password'})
        self.assertEqual(response.status,200,await response.text())
        cookie=response.cookies['__Secure-relay_session']
        self.assertEqual(cookie['path'],'/relay/domains/studio/')
        headers={'Cookie':'__Secure-relay_session='+cookie.value+'; __Secure-aster_squad_session=never-forward; unrelated-app=never-forward'}
        response=await self.client.get('/relay/domains/studio/api/state',headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['me']['username'],'user')
        forwarded=self.observed[-1].get('Cookie','')
        self.assertIn('__Secure-relay_session=',forwarded)
        self.assertNotIn('aster_squad',forwarded);self.assertNotIn('unrelated-app',forwarded)
        self.assertEqual((await self.client.get('/squad/api/state',headers=headers)).status,401)
        response=await self.client.post('/relay/domains/studio/api/domain',headers=headers,json={'name':'forbidden'})
        self.assertEqual(response.status,403)
        response=await self.client.post('/relay/domains/studio/api/auth/logout',headers={**headers,'Origin':origin},json={})
        self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get('/relay/domains/studio/api/state',headers=headers)).status,401)

    async def test_proxy_preserves_sender_and_drops_other_app_cookies(self):
        headers = {**self.header(self.relay_token),"Cookie":"unrelated-app-session=test-only", "X-Forwarded-Host":"untrusted.example", "Origin":"https://mobile.example.test:8443"}
        response = await self.client.post("/relay/domains/studio/api/messages",headers=headers,
            json={"to":"@person","text":"phone relay","sender":"forged"})
        self.assertEqual(response.status,200)
        row = await response.json()
        self.assertEqual(row["sender"],self.owner)
        self.assertNotIn("Cookie",self.observed[-1])
        self.assertNotIn("X-Forwarded-Host",self.observed[-1])
        response = await self.client.get("/relay/domains/studio/api/messages?peer=person&limit=1",headers=self.header(self.relay_token))
        self.assertEqual(len((await response.json())["messages"]),1)
        response = await self.client.post("/relay/domains/studio/api/messages",headers={**headers,"Origin":"https://untrusted.example"},json={"to":"person","text":"must fail"})
        self.assertEqual(response.status,403)
        self.assertEqual(len(self.store.history(self.owner,"person")),1)



if __name__ == "__main__":
    unittest.main()
