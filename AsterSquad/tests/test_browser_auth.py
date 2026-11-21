"""Single-owner setup authorization, password verification and cookie lifecycle."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aiohttp import CookieJar,web
from aiohttp.test_utils import TestClient,TestServer
from relay.server import errors
from squad.browser_auth import BrowserAuth, COOKIE, CHALLENGE_COOKIE

ORIGIN='https://squad.example.test:8443'

class BrowserAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'auth.db'
        self.auth=BrowserAuth(self.path,ORIGIN,'test-owner-token')
        app=web.Application(middlewares=[errors]);app.router.add_route('*','/squad/auth/{action:.*}',self.auth.handler)
        self.client=TestClient(TestServer(app),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self,path,body=None,token=False,cookie=None,origin=ORIGIN):
        headers={'Origin':origin}
        if token:headers['Authorization']='Bearer test-owner-token'
        if cookie:headers['Cookie']=COOKIE+'='+cookie
        return await self.client.post('/squad/auth/'+path,json=body or {},headers=headers)

    async def setup(self):
        response=await self.post('password/set',{'username':'myuser','password':'a-long-test-password'},token=True)
        self.assertEqual(response.status,200)
        return response.cookies[COOKIE].value

    async def test_setup_rejects_anonymous_cross_origin_and_short_password(self):
        body={'username':'myuser','password':'a-long-test-password'}
        self.assertEqual((await self.post('password/set',body)).status,401)
        self.assertEqual((await self.post('password/set',body,token=True,origin='https://evil.test')).status,403)
        self.assertEqual((await self.post('password/set',{'username':'x','password':'short'},token=True)).status,400)
        self.assertIsNone(self.auth.owner())
        await self.setup()
        owner=self.auth.owner()
        self.assertNotEqual(owner['password'],body['password'])
        self.assertNotIn(body['password'].encode(),self.path.read_bytes())
        import os
        if os.name == 'nt':
            import win32security
            descriptor=win32security.GetNamedSecurityInfo(str(self.path),win32security.SE_FILE_OBJECT,win32security.DACL_SECURITY_INFORMATION)
            self.assertTrue(descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
        else:
            self.assertEqual(self.path.stat().st_mode&0o777,0o600)

    async def test_password_change_revocation_logout_and_restart(self):
        initial=await self.setup()
        response=await self.post('password/login',{'username':'myuser','password':'wrong'})
        self.assertEqual(response.status,401)
        response=await self.post('password/login',{'username':'myuser','password':'a-long-test-password'})
        self.assertEqual(response.status,200)
        cookie=response.cookies[COOKIE]
        self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly']);self.assertEqual(cookie['samesite'],'Strict')
        session=cookie.value
        restart=BrowserAuth(self.path,ORIGIN,'test-owner-token')
        request=SimpleNamespace(cookies={COOKIE:session})
        self.assertIsNotNone(restart.session(request))
        changed={'username':'myuser','password':'a-new-long-password','current_password':'wrong'}
        self.assertEqual((await self.post('password/set',changed,cookie=session)).status,401)
        changed['current_password']='a-long-test-password'
        response=await self.post('password/set',changed,cookie=session)
        self.assertEqual(response.status,200)
        current=response.cookies[COOKIE].value
        self.assertIsNone(self.auth.session(request))
        self.assertIsNone(self.auth.session(SimpleNamespace(cookies={COOKIE:initial})))
        self.assertEqual((await self.post('logout',cookie=current)).status,200)
        self.assertIsNone(self.auth.session(SimpleNamespace(cookies={COOKIE:current})))

    async def test_throttle_and_cookie_is_not_a_bearer(self):
        await self.setup()
        for _ in range(20):
            self.assertEqual((await self.post('password/login',{'username':'myuser','password':'bad'})).status,401)
        self.assertEqual((await self.post('password/login',{'username':'myuser','password':'bad'})).status,429)
        self.assertIsNone(self.auth.session(SimpleNamespace(cookies={COOKIE:'test-owner-token'})))

    async def test_challenge_is_single_use_origin_bound_and_requires_identity(self):
        session=await self.setup()
        self.assertEqual((await self.post('passkeys/register/options')).status,401)
        response=await self.post('passkeys/register/options',cookie=session)
        self.assertEqual(response.status,200)
        options=await response.json()
        self.assertEqual(options['rp']['id'],'squad.example.test')
        self.assertEqual(options['authenticatorSelection']['userVerification'],'required')
        challenge=response.cookies[CHALLENGE_COOKIE].value
        request=SimpleNamespace(cookies={COOKIE:session,CHALLENGE_COOKIE:challenge},headers={})
        self.assertTrue(self.auth.take_challenge(request,'register'))
        with self.assertRaises(Exception):self.auth.take_challenge(request,'register')
        response=await self.post('passkeys/login/options')
        self.assertEqual(response.status,409)
