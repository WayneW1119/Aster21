"""Relay owns password/session authentication without a Squad dependency."""
from pathlib import Path
import tempfile
import unittest
from aiohttp.test_utils import TestClient,TestServer
from relay.server import Service,make_app
from relay.store import Store
from relay.browser_auth import COOKIE

ORIGIN='https://relay.example.test:8443'

class BrowserAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/'relay.db');self.owner=self.store.create(None,'user');self.child=self.store.create(self.owner['account']['id'],'child')
        self.client=TestClient(TestServer(make_app(Service(self.store,ORIGIN+'/relay/domains/test'))))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close();self.store.db.close();self.temp.cleanup()
    async def setup_password(self,token,password='a-long-relay-password'):
        r=await self.client.post('/api/auth/password/set',headers={'Authorization':'Bearer '+token,'Origin':ORIGIN},json={'password':password})
        self.assertEqual(r.status,200,await r.text())
        self.assertEqual(r.cookies[COOKIE]['path'],'/relay/domains/test/')
        self.assertTrue(r.cookies[COOKIE]['httponly']);self.assertTrue(r.cookies[COOKIE]['secure'])
        return { 'Cookie':COOKIE+'='+r.cookies[COOKIE].value }
    async def test_password_login_sessions_origin_and_no_token_disclosure(self):
        cookie=await self.setup_password(self.owner['token'])
        r=await self.client.get('/api/state',headers=cookie);self.assertEqual(r.status,200)
        self.assertNotIn(self.owner['token'],await r.text())
        self.assertEqual((await self.client.post('/api/domain',headers=cookie,json={'name':'CSRF'})).status,403)
        self.assertEqual((await self.client.post('/api/auth/password/login',headers={'Origin':'https://wrong.test'},json={})).status,403)
        for password,status in [('incorrect',401),('a-long-relay-password',200)]:
            r=await self.client.post('/api/auth/password/login',headers={'Origin':ORIGIN},json={'username':'user','password':password})
            self.assertEqual(r.status,status)
        r=await self.client.post('/api/auth/logout',headers={**cookie,'Origin':ORIGIN},json={});self.assertEqual(r.status,200)
        self.assertEqual((await self.client.get('/api/state',headers=cookie)).status,401)
    async def test_accounts_cannot_edit_each_other_and_disabled_ancestor_revokes_sessions(self):
        owner_cookie=await self.setup_password(self.owner['token'])
        child_cookie=await self.setup_password(self.child['token'],'a-different-password')
        r=await self.client.get('/api/auth/status',headers=child_cookie)
        self.assertEqual((await r.json())['username'],'child')
        self.store.manage(self.owner['account']['id'],self.child['account']['id'],'disable')
        self.assertEqual((await self.client.get('/api/state',headers=child_cookie)).status,401)
        self.assertEqual((await self.client.get('/api/state',headers=owner_cookie)).status,200)
        r=await self.client.post('/api/auth/password/login',headers={'Origin':ORIGIN},json={'username':'child','password':'a-different-password'})
        self.assertEqual(r.status,401)
    async def test_password_change_revokes_only_this_accounts_sessions(self):
        owner=await self.setup_password(self.owner['token']);child=await self.setup_password(self.child['token'])
        r=await self.client.post('/api/auth/password/set',headers={**child,'Origin':ORIGIN},json={'password':'new-child-password','current_password':'incorrect'})
        self.assertEqual(r.status,401)
        r=await self.client.post('/api/auth/password/set',headers={**child,'Origin':ORIGIN},json={'password':'new-child-password','current_password':'a-long-relay-password'})
        self.assertEqual(r.status,200)
        self.assertEqual((await self.client.get('/api/state',headers=child)).status,401)
        self.assertEqual((await self.client.get('/api/state',headers=owner)).status,200)
    async def test_squad_cookies_and_other_domain_sessions_do_not_authenticate(self):
        cookie=await self.setup_password(self.owner['token'])
        fake={'Cookie':cookie['Cookie'].replace(COOKIE,'__Secure-aster_squad_session')}
        self.assertEqual((await self.client.get('/api/state',headers=fake)).status,401)
        other=Store(self.root/'other/relay.db');other.create(None,'user')
        other_client=TestClient(TestServer(make_app(Service(other,ORIGIN+'/relay/domains/other'))));await other_client.start_server()
        try:self.assertEqual((await other_client.get('/api/state',headers=cookie)).status,401)
        finally:await other_client.close();other.db.close()
    async def test_registration_requires_account_and_recovery_password(self):
        r=await self.client.post('/api/auth/passkeys/register/options',headers={'Origin':ORIGIN},json={});self.assertEqual(r.status,401)
        r=await self.client.post('/api/auth/passkeys/register/options',headers={'Origin':ORIGIN,'Authorization':'Bearer '+self.owner['token']},json={});self.assertEqual(r.status,409)

    async def test_file_upload_cookie_origin_and_authenticated_download(self):
        from aiohttp import FormData
        cookie = await self.setup_password(self.owner['token'])
        async def upload(headers, ident):
            form = FormData(quote_fields=False)
            form.add_field('file', b'private', filename='private.txt')
            return await self.client.post('/api/files?to=child&client_id='+ident, data=form, headers=headers)
        self.assertEqual((await upload(cookie, 'no-origin')).status, 403)
        self.assertEqual((await upload({**cookie, 'Origin':'https://wrong.test'}, 'wrong-origin')).status, 403)
        response = await upload({**cookie, 'Origin':ORIGIN}, 'valid-origin')
        self.assertEqual(response.status, 200, await response.text())
        attachment = (await response.json())['attachment']
        response = await self.client.get('/api/files/'+attachment['id'], headers=cookie)
        self.assertEqual(await response.read(), b'private')
        self.assertEqual(response.status, 200)
