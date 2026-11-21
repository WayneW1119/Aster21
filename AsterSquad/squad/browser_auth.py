"""Single-owner password and WebAuthn login, separate from model/service credentials."""
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from aiohttp import web
from relay.store import Problem

COOKIE = '__Secure-aster_squad_session'
CHALLENGE_COOKIE = '__Secure-aster_squad_challenge'
COOKIE_PATH = '/squad/'
LIFETIME = 30 * 24 * 3600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64*1024*1024).hex()


class BrowserAuth:
    def __init__(self, path, origin, owner_token):
        self.path, self.origin, self.owner_token = path, origin, owner_token
        self.rp_id = urlsplit(origin).hostname
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS owner(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT,
                    salt BLOB, password TEXT, user_id BLOB);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, created REAL, expires REAL);
                CREATE TABLE IF NOT EXISTS passkeys(id TEXT PRIMARY KEY, public_key BLOB, sign_count INTEGER,
                    name TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, kind TEXT, value BLOB,
                    session TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS attempts(at REAL, kind TEXT);
            ''')
        os.chmod(self.path, 0o600)
        if os.name == "nt":
            from squad._windows import private_file
            private_file(self.path)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def owner(self):
        with self.db() as db:
            return db.execute('SELECT * FROM owner WHERE id=1').fetchone()

    def bearer(self, request):
        return hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.owner_token)

    def session(self, request):
        value = request.cookies.get(COOKIE, '')
        if len(value) != 43:
            return None
        with self.db() as db:
            return db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (digest(value),time.time())).fetchone()

    def require(self, request):
        if not self.bearer(request) and not self.session(request):
            raise Problem('请先登录工作台', 401)

    def origin_check(self, request):
        if request.headers.get('Origin') != self.origin:
            raise Problem('请从工作台页面重试', 403)

    def limit(self, kind, limit=20):
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE at<?',(now-300,))
            if db.execute('SELECT count(*) FROM attempts WHERE kind=?',(kind,)).fetchone()[0] >= limit:
                raise Problem('尝试过于频繁，请五分钟后重试',429)
            db.execute('INSERT INTO attempts VALUES (?,?)',(now,kind))

    def check_password(self, username, password):
        owner=self.owner()
        salt=owner['salt'] if owner else b'nonexistent-owner'
        candidate=password_hash(password,salt)
        return bool(owner and hmac.compare_digest(username.encode(),owner['username'].encode())
                    and hmac.compare_digest(candidate,owner['password']))

    def issue(self):
        value=secrets.token_urlsafe(32);now=time.time()
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<=?',(now,))
            db.execute('INSERT INTO sessions VALUES (?,?,?)',(digest(value),now,now+LIFETIME))
        response=web.json_response({'ok':True})
        response.set_cookie(COOKIE,value,path=COOKIE_PATH,max_age=LIFETIME,secure=True,httponly=True,samesite='Strict')
        return response

    async def recent(self, request, body):
        self.require(request)
        if self.bearer(request):
            return
        session=self.session(request)
        if session and session['created']>time.time()-600:
            return
        self.limit('password')
        owner=self.owner()
        if not owner or not await asyncio.to_thread(self.check_password,owner['username'],str(body.get('current_password',''))[:1025]):
            raise Problem('为保护登录设置，请输入当前密码后重试',401)

    def options_response(self, options, kind, request):
        from webauthn import options_to_json
        value=secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM challenges WHERE expires<=?',(time.time(),))
            db.execute('INSERT INTO challenges VALUES (?,?,?,?,?)',
                (digest(value),kind,options.challenge,
                 self.session(request)['token'] if self.session(request) else ('bearer' if self.bearer(request) else ''),time.time()+180))
        response=web.json_response(json.loads(options_to_json(options)))
        response.set_cookie(CHALLENGE_COOKIE,value,path=COOKIE_PATH,max_age=180,secure=True,httponly=True,samesite='Strict')
        return response

    def take_challenge(self, request, kind):
        key=digest(request.cookies.get(CHALLENGE_COOKIE,''))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM challenges WHERE id=?',(key,)).fetchone()
            db.execute('DELETE FROM challenges WHERE id=?',(key,))
        if not row or row['kind']!=kind or row['expires']<=time.time():
            raise Problem('验证已过期或已使用，请重试',400)
        if kind=='register':
            session=self.session(request)
            binding=session['token'] if session else ('bearer' if self.bearer(request) else '')
            if not binding or not hmac.compare_digest(binding,row['session']):
                raise Problem('登录状态已改变，请重试',401)
        return row['value']

    async def handler(self, request):
        path=request.match_info['action']
        if request.method=='POST':
            self.origin_check(request)
        if path=='status' and request.method=='GET':
            owner=self.owner();authenticated=self.bearer(request) or bool(self.session(request))
            with self.db() as db:
                keys=[dict(row) for row in db.execute('SELECT id,name,created FROM passkeys ORDER BY created')]
            return web.json_response({'configured':bool(owner),'authenticated':authenticated,
                'passkeys_available': bool(keys), 'passkeys_supported':self.origin.startswith('https://') or urlsplit(self.origin).hostname=='localhost',
                'username':owner['username'] if owner and authenticated else '',
                'passkeys':keys if authenticated else []})
        if request.method!='POST':
            raise Problem('未知登录接口',404)
        body=await request.json()
        if not isinstance(body,dict):
            raise Problem('请求格式无效',400)
        if path=='password/login':
            self.limit('password')
            username, password=body.get('username',''),body.get('password','')
            if not isinstance(username,str) or not isinstance(password,str) or len(username)>80 or len(password)>1024:
                raise Problem('账号或密码不正确',401)
            if not await asyncio.to_thread(self.check_password,username,password):
                raise Problem('账号或密码不正确',401)
            return self.issue()
        if path=='password/set':
            self.require(request)
            owner=self.owner()
            if owner and not self.bearer(request):
                self.limit('password')
                if not await asyncio.to_thread(self.check_password,owner['username'],str(body.get('current_password',''))[:1025]):
                    raise Problem('当前密码不正确',401)
            username,password=body.get('username',''),body.get('password','')
            if not isinstance(username,str) or not 1<=len(username.strip())<=80:
                raise Problem('账号应为 1–80 个字符',400)
            if not isinstance(password,str) or not 12<=len(password)<=1024:
                raise Problem('密码至少 12 个字符，最多 1024 个字符',400)
            salt=secrets.token_bytes(16)
            hashed=await asyncio.to_thread(password_hash,password,salt)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO owner VALUES (1,?,?,?,?)',
                    (username.strip(),salt,hashed,owner['user_id'] if owner else secrets.token_bytes(32)))
                db.execute('DELETE FROM sessions')
                db.execute('DELETE FROM challenges')
            return self.issue()
        if path=='logout':
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE token=?',(digest(request.cookies.get(COOKIE,'')),))
            response=web.json_response({'ok':True})
            response.set_cookie(COOKIE,'',max_age=0,path=COOKIE_PATH,secure=True,httponly=True,samesite='Strict')
            response.set_cookie(CHALLENGE_COOKIE,'',max_age=0,path=COOKIE_PATH,secure=True,httponly=True,samesite='Strict')
            return response
        if path=='passkeys/register/options':
            await self.recent(request,body)
            owner=self.owner()
            if not owner:
                raise Problem('请先设置账号密码作为恢复方式',409)
            self.limit('register',20)
            from webauthn import generate_registration_options
            from webauthn.helpers.structs import AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement, PublicKeyCredentialDescriptor
            with self.db() as db:
                keys=[PublicKeyCredentialDescriptor(id=decode(row[0])) for row in db.execute('SELECT id FROM passkeys')]
            if len(keys)>=10:
                raise Problem('最多保留 10 个通行密钥，请先移除不用的密钥',409)
            options=generate_registration_options(rp_id=self.rp_id,rp_name='Aster Squad',
                user_id=owner['user_id'],user_name=owner['username'],exclude_credentials=keys,
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                    user_verification=UserVerificationRequirement.REQUIRED))
            return self.options_response(options,'register',request)
        if path=='passkeys/register/verify':
            self.require(request)
            challenge=self.take_challenge(request,'register')
            from webauthn import verify_registration_response
            try:
                result=verify_registration_response(credential=body['credential'],expected_challenge=challenge,
                    expected_rp_id=self.rp_id,expected_origin=self.origin,require_user_verification=True)
            except Exception:
                raise Problem('通行密钥验证失败，请重新绑定',400) from None
            name=str(body.get('name','我的通行密钥')).strip()[:80] or '我的通行密钥'
            with self.db() as db:
                try:
                    db.execute('INSERT INTO passkeys VALUES (?,?,?,?,?)',
                        (encode(result.credential_id),result.credential_public_key,result.sign_count,name,time.time()))
                except sqlite3.IntegrityError:
                    raise Problem('此通行密钥已绑定',409) from None
            response=web.json_response({'ok':True})
            response.set_cookie(CHALLENGE_COOKIE,'',max_age=0,path=COOKIE_PATH,secure=True,httponly=True,samesite='Strict')
            return response
        if path=='passkeys/login/options':
            self.limit('passkey',60)
            from webauthn import generate_authentication_options
            from webauthn.helpers.structs import UserVerificationRequirement,PublicKeyCredentialDescriptor
            with self.db() as db:
                keys=[PublicKeyCredentialDescriptor(id=decode(row[0])) for row in db.execute('SELECT id FROM passkeys')]
            if not keys:
                raise Problem('请先用密码或管理凭据登录并绑定通行密钥',409)
            options=generate_authentication_options(rp_id=self.rp_id,allow_credentials=keys,
                user_verification=UserVerificationRequirement.REQUIRED)
            return self.options_response(options,'login',request)
        if path=='passkeys/login/verify':
            self.limit('passkey_verify',30)
            challenge=self.take_challenge(request,'login')
            credential=body.get('credential',{})
            if not isinstance(credential,dict):
                raise Problem('通行密钥验证失败',400)
            with self.db() as db:
                row=db.execute('SELECT * FROM passkeys WHERE id=?',(credential.get('id',''),)).fetchone()
            if not row:
                raise Problem('通行密钥验证失败',400)
            from webauthn import verify_authentication_response
            try:
                result=verify_authentication_response(credential=credential,expected_challenge=challenge,
                    expected_rp_id=self.rp_id,expected_origin=self.origin,credential_public_key=row['public_key'],
                    credential_current_sign_count=row['sign_count'],require_user_verification=True)
            except Exception:
                raise Problem('通行密钥验证失败，请重试',400) from None
            owner=self.owner()
            user_handle=(credential.get('response') or {}).get('userHandle')
            if user_handle is not None and (not isinstance(user_handle,str) or not hmac.compare_digest(user_handle,encode(owner['user_id']))):
                raise Problem('通行密钥账号不匹配',400)
            with self.db() as db:
                db.execute('UPDATE passkeys SET sign_count=? WHERE id=?',(result.new_sign_count,row['id']))
            response=self.issue()
            response.set_cookie(CHALLENGE_COOKIE,'',max_age=0,path=COOKIE_PATH,secure=True,httponly=True,samesite='Strict')
            return response
        if path=='passkeys/remove':
            await self.recent(request,body)
            with self.db() as db:
                db.execute('DELETE FROM passkeys WHERE id=?',(str(body.get('id','')),))
                # A removed key must not leave existing sessions trusted.
                db.execute('DELETE FROM sessions')
                db.execute('DELETE FROM challenges')
            return self.issue()
        raise Problem('未知登录接口',404)
