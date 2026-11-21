"""Domain-local browser accounts. Machine bearer tokens remain independent.

Cookies contain random session IDs, never Relay API tokens. All credentials are
bound to an existing enabled domain account; this module has no Squad imports.
"""
import asyncio
import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import urlsplit

from aiohttp import web
from .store import Problem

COOKIE = '__Secure-relay_session'
CHALLENGE_COOKIE = '__Secure-relay_challenge'
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
    def __init__(self, store, public_url):
        self.store = store
        url = urlsplit(public_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Relay public URL must be an absolute HTTP(S) URL')
        self.origin = url.scheme + '://' + url.netloc
        self.rp_id = url.hostname
        self.cookie_path = url.path.rstrip('/') + '/'
        self.secure = url.scheme == 'https'
        self.cookie = COOKIE if self.secure else 'relay_session'
        self.challenge_cookie = CHALLENGE_COOKIE if self.secure else 'relay_challenge'
        # A separate DB keeps credential records out of domain content exports.
        database = store.db.execute('PRAGMA database_list').fetchone()[2]
        self.path = Path(database).parent / 'browser-auth.sqlite3'
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS users(account TEXT PRIMARY KEY, salt BLOB, password TEXT, user_id BLOB);
              CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, account TEXT, created REAL, expires REAL);
              CREATE TABLE IF NOT EXISTS passkeys(id TEXT PRIMARY KEY, account TEXT, public_key BLOB, sign_count INTEGER, name TEXT, created REAL);
              CREATE TABLE IF NOT EXISTS challenges(id TEXT PRIMARY KEY, kind TEXT, account TEXT, value BLOB, binding TEXT, expires REAL);
              CREATE TABLE IF NOT EXISTS attempts(at REAL, kind TEXT);
            ''')
        os.chmod(self.path, 0o600)
        if os.name == "nt":
            from relay._windows import private_file
            private_file(self.path)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def user(self, account):
        with self.db() as db:
            row = db.execute('SELECT * FROM users WHERE account=?', (account,)).fetchone()
        return row

    def bearer(self, request):
        raw = request.headers.get('Authorization', '')
        if not raw.startswith('Bearer ') or not raw[7:]:
            return None
        return self.store.authenticate(raw[7:])['id']

    def session(self, request):
        value = request.cookies.get(self.cookie, '')
        if len(value) != 43:
            return None
        with self.db() as db:
            row = db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (digest(value), time.time())).fetchone()
        if not row or not self.store.enabled(row['account']):
            return None
        return row

    def actor(self, request, *, required=True):
        actor = self.bearer(request)
        if not actor:
            session = self.session(request)
            actor = session['account'] if session else None
        if not actor and required:
            raise Problem('请先登录这个 Relay 域', 401)
        return actor

    def origin_check(self, request):
        if request.headers.get('Origin') != self.origin:
            raise Problem('请从当前 Relay 页面重试', 403)

    def limit(self, kind, maximum=20):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM attempts WHERE at<?', (time.time()-300,))
            if db.execute('SELECT count(*) FROM attempts WHERE kind=?', (kind,)).fetchone()[0] >= maximum:
                raise Problem('尝试过于频繁，请五分钟后重试', 429)
            db.execute('INSERT INTO attempts VALUES (?,?)', (time.time(), kind))

    async def password_matches(self, account, password):
        user = self.user(account) if account else None
        candidate = await asyncio.to_thread(password_hash, password, user['salt'] if user else b'no-relay-account')
        return bool(user and hmac.compare_digest(candidate, user['password']))

    def set_cookie(self, response, name, value, max_age):
        response.set_cookie(name, value, path=self.cookie_path, max_age=max_age,
            secure=self.secure, httponly=True, samesite='Strict')

    def issue(self, account):
        if not self.store.enabled(account):
            raise Problem('账号已停用', 401)
        value = secrets.token_urlsafe(32); now = time.time()
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
            db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest(value), account, now, now+LIFETIME))
        response = web.json_response({'ok': True})
        self.set_cookie(response, self.cookie, value, LIFETIME)
        return response

    async def recent(self, request, body, account):
        if self.bearer(request):
            return
        session = self.session(request)
        if session and session['created'] > time.time()-600:
            return
        self.limit('password')
        if not await self.password_matches(account, str(body.get('current_password', ''))[:1025]):
            raise Problem('请填写当前密码后重试', 401)

    def options_response(self, options, kind, account, request):
        from webauthn import options_to_json
        value = secrets.token_urlsafe(32)
        session = self.session(request)
        binding = session['token'] if session else ('bearer:'+account if account and self.bearer(request) else '')
        with self.db() as db:
            db.execute('DELETE FROM challenges WHERE expires<=?', (time.time(),))
            db.execute('INSERT INTO challenges VALUES (?,?,?,?,?,?)',
                (digest(value), kind, account, options.challenge, binding, time.time()+180))
        response = web.json_response(json.loads(options_to_json(options)))
        self.set_cookie(response, self.challenge_cookie, value, 180)
        return response

    def take_challenge(self, request, kind, account=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM challenges WHERE id=?', (digest(request.cookies.get(self.challenge_cookie, '')),)).fetchone()
            db.execute('DELETE FROM challenges WHERE id=?', (digest(request.cookies.get(self.challenge_cookie, '')),))
        if not row or row['kind'] != kind or row['expires'] <= time.time():
            raise Problem('验证已过期或已使用，请重试', 400)
        if kind == 'register':
            session = self.session(request)
            binding = session['token'] if session else ('bearer:'+account if account and self.bearer(request) else '')
            if row['account'] != account or not binding or not hmac.compare_digest(row['binding'], binding):
                raise Problem('登录状态已改变，请重试', 401)
        return row

    async def handler(self, request):
        action = request.match_info['action']
        if request.method == 'POST':
            self.origin_check(request)
        account = self.actor(request, required=False)
        if action == 'status' and request.method == 'GET':
            keys = []
            if account:
                with self.db() as db:
                    keys = [dict(r) for r in db.execute('SELECT id,name,created FROM passkeys WHERE account=? ORDER BY created', (account,))]
            return web.json_response({'authenticated': bool(account), 'configured': bool(account and self.user(account)),
                'username': self.store.account(account)['username'] if account else '', 'domain': self.store.domain(),
                'passkeys': keys, 'passkeys_supported': self.secure or self.rp_id == 'localhost'})
        if request.method != 'POST':
            raise Problem('未知登录接口', 404)
        body = await request.json()
        if not isinstance(body, dict):
            raise Problem('请求格式无效', 400)
        if action == 'password/login':
            self.limit('password')
            username, password = body.get('username', ''), body.get('password', '')
            if not isinstance(username, str) or not isinstance(password, str) or len(username)>80 or len(password)>1024:
                raise Problem('账号或密码不正确', 401)
            row = self.store.one('SELECT id FROM accounts WHERE handle=?', (username.strip().removeprefix('@').lower(),))
            ident = row['id'] if row else None
            if not await self.password_matches(ident, password) or not self.store.enabled(ident):
                raise Problem('账号或密码不正确', 401)
            return self.issue(ident)
        if action == 'logout':
            with self.db() as db:
                db.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get(self.cookie, '')),))
            response = web.json_response({'ok': True})
            self.set_cookie(response, self.cookie, '', 0); self.set_cookie(response, self.challenge_cookie, '', 0)
            return response
        if action == 'passkeys/login/options':
            self.limit('passkey', 60)
            from webauthn import generate_authentication_options
            from webauthn.helpers.structs import UserVerificationRequirement
            # Discoverable credentials: the system chooses an account; do not
            # publish this domain's credential IDs to anonymous callers.
            options = generate_authentication_options(rp_id=self.rp_id,
                user_verification=UserVerificationRequirement.REQUIRED)
            return self.options_response(options, 'login', None, request)
        if action == 'passkeys/login/verify':
            self.limit('passkey_verify', 30)
            challenge = self.take_challenge(request, 'login')
            credential = body.get('credential', {})
            if not isinstance(credential, dict):
                raise Problem('通行密钥验证失败', 400)
            with self.db() as db:
                row = db.execute('SELECT * FROM passkeys WHERE id=?', (credential.get('id', ''),)).fetchone()
            if not row or not self.store.enabled(row['account']):
                raise Problem('通行密钥验证失败', 400)
            from webauthn import verify_authentication_response
            try:
                result = verify_authentication_response(credential=credential, expected_challenge=challenge['value'],
                    expected_rp_id=self.rp_id, expected_origin=self.origin, credential_public_key=row['public_key'],
                    credential_current_sign_count=row['sign_count'], require_user_verification=True)
                handle = (credential.get('response') or {}).get('userHandle')
                user = self.user(row['account'])
                if not user or not isinstance(handle, str) or not hmac.compare_digest(handle, encode(user['user_id'])):
                    raise ValueError('Account mismatch')
            except Exception:
                raise Problem('通行密钥验证失败，请重试', 400) from None
            with self.db() as db:
                db.execute('UPDATE passkeys SET sign_count=? WHERE id=?', (result.new_sign_count, row['id']))
            response = self.issue(row['account']); self.set_cookie(response, self.challenge_cookie, '', 0)
            return response
        if not account:
            raise Problem('请先登录这个 Relay 域', 401)
        if action == 'password/set':
            user = self.user(account)
            if user and not self.bearer(request):
                self.limit('password')
                if not await self.password_matches(account, str(body.get('current_password', ''))[:1025]):
                    raise Problem('当前密码不正确', 401)
            password = body.get('password', '')
            if not isinstance(password, str) or not 12 <= len(password) <= 1024:
                raise Problem('密码应为 12–1024 个字符', 400)
            salt = secrets.token_bytes(16)
            hashed = await asyncio.to_thread(password_hash, password, salt)
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO users VALUES (?,?,?,?)',
                    (account, salt, hashed, user['user_id'] if user else secrets.token_bytes(32)))
                db.execute('DELETE FROM sessions WHERE account=?', (account,))
                db.execute('DELETE FROM challenges WHERE account=?', (account,))
            return self.issue(account)
        if action == 'passkeys/register/options':
            await self.recent(request, body, account)
            user = self.user(account)
            if not user:
                raise Problem('请先设置 Relay 密码作为恢复方式', 409)
            self.limit('register')
            from webauthn import generate_registration_options
            from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, ResidentKeyRequirement,
                UserVerificationRequirement, PublicKeyCredentialDescriptor)
            with self.db() as db:
                keys = [PublicKeyCredentialDescriptor(id=decode(r[0])) for r in db.execute('SELECT id FROM passkeys WHERE account=?', (account,))]
            if len(keys) >= 10:
                raise Problem('最多保留 10 个通行密钥', 409)
            options = generate_registration_options(rp_id=self.rp_id, rp_name='Relay', user_id=user['user_id'],
                user_name=self.store.account(account)['username'],
                user_display_name=self.store.account(account)['username']+' · '+self.store.domain()['name'],
                exclude_credentials=keys, authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED))
            return self.options_response(options, 'register', account, request)
        if action == 'passkeys/register/verify':
            challenge = self.take_challenge(request, 'register', account)
            from webauthn import verify_registration_response
            try:
                result = verify_registration_response(credential=body['credential'], expected_challenge=challenge['value'],
                    expected_rp_id=self.rp_id, expected_origin=self.origin, require_user_verification=True)
            except Exception:
                raise Problem('通行密钥验证失败，请重新绑定', 400) from None
            with self.db() as db:
                try:
                    db.execute('INSERT INTO passkeys VALUES (?,?,?,?,?,?)', (encode(result.credential_id), account,
                        result.credential_public_key, result.sign_count, str(body.get('name', '我的设备')).strip()[:80], time.time()))
                except sqlite3.IntegrityError:
                    raise Problem('此通行密钥已绑定', 409) from None
            response = web.json_response({'ok': True}); self.set_cookie(response, self.challenge_cookie, '', 0)
            return response
        if action == 'passkeys/remove':
            await self.recent(request, body, account)
            with self.db() as db:
                db.execute('DELETE FROM passkeys WHERE id=? AND account=?', (str(body.get('id', '')), account))
                db.execute('DELETE FROM sessions WHERE account=?', (account,))
                db.execute('DELETE FROM challenges WHERE account=?', (account,))
            return self.issue(account)
        raise Problem('未知登录接口', 404)
