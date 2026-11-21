"""Official Sign in with ChatGPT public-client flow for local personal apps.

No Codex credentials, browser cookies, API-key fallback or backend-api endpoints.
"""

from contextlib import contextmanager
import base64
try:
    import fcntl
except ImportError:
    from agent_harness import _windows_locks as fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
if os.name == "nt":
    from agent_harness._windows import is_reparse
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4
import webbrowser

from ..core.client_common import RetryableModelError

ISSUER = 'https://auth.openai.com'
AUTHORIZE = ISSUER + '/api/accounts/authorize'
TOKEN = ISSUER + '/api/accounts/oauth/token'
JWKS = ISSUER + '/.well-known/jwks.json'
REVOKE = ISSUER + '/api/accounts/oauth/revoke'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
_CLOCK_SKEW_SECONDS = 5


class ChatGPTAuthError(ValueError):
    pass


class ChatGPTTransientAuthError(ChatGPTAuthError, RetryableModelError):
    """Temporary token/JWKS transport failure; identity verification stays mandatory."""


class _ExpiredIdentity(ChatGPTAuthError):
    """A quarantined refresh response may need renewal, never activation."""


def _request_json(url, *, form=None, token=None):
    headers = {'Accept': 'application/json'}
    data = None
    if form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        data = urlencode(form).encode()
    if token is not None:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ChatGPTAuthError('Authentication response is too large')
            return json.loads(raw) if raw else {}
    except HTTPError as error:
        # Provider bodies can contain credentials or user info. Never echo them.
        kind = ChatGPTTransientAuthError if error.code == 429 or error.code >= 500 else ChatGPTAuthError
        raise kind(f'OpenAI returned HTTP {error.code}; check authorization and account access') from None
    except (URLError, TimeoutError) as error:
        raise ChatGPTTransientAuthError('Could not complete the OpenAI authentication request') from None
    except json.JSONDecodeError as error:
        raise ChatGPTAuthError('Could not complete the OpenAI authentication request') from None


def _validate_identity(encoded, client_id, nonce=None):
    try:
        import jwt
    except ImportError:
        raise ChatGPTAuthError('Install the chatgpt extra: pip install -e ".[chatgpt]"') from None
    try:
        try:
            key = jwt.PyJWKClient(JWKS).get_signing_key_from_jwt(encoded)
        except (jwt.PyJWKClientError, jwt.PyJWKSetError, ValueError, OSError) as error:
            # A failed key lookup is not evidence that the identity is invalid.
            # Retain the rotated response and let the bounded request retry own timing.
            raise ChatGPTTransientAuthError(
                f'Could not obtain OpenAI identity signing keys ({type(error).__name__}); credentials were not activated') from None
        identity = jwt.decode(encoded, key.key, algorithms=['RS256'], issuer=ISSUER,
            audience=client_id, leeway=_CLOCK_SKEW_SECONDS,
            options={'require': ['sub', 'exp', 'iss', 'aud']})
        if not isinstance(identity['sub'], str) or not identity['sub']:
            raise ValueError('Missing subject')
        if nonce is not None and (not isinstance(identity.get('nonce'), str)
                or not hmac.compare_digest(identity['nonce'], nonce)):
            raise ValueError('Invalid nonce')
        return identity
    except ChatGPTTransientAuthError:
        raise
    except jwt.ExpiredSignatureError:
        raise _ExpiredIdentity(
            'OpenAI ID token validation failed (ExpiredSignatureError); credentials were not activated') from None
    except Exception as error:
        raise ChatGPTAuthError(
            f'OpenAI ID token validation failed ({type(error).__name__}); credentials were not activated') from None


class ChatGPTAccount:
    def __init__(self, home, profile='default'):
        if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', profile):
            raise ChatGPTAuthError('Profile must contain 1-64 letters, digits, _ or -')
        self.root = Path(home).expanduser().absolute() / 'credentials' / 'chatgpt'
        self.profile = profile
        self.path = self.root / (profile + '.json')

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == 'nt' and any(p.is_symlink() or is_reparse(p) for p in (self.root / '.lock',self.root,*self.root.parents)):
            raise ChatGPTAuthError('Credential lock must not cross reparse points')
        fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR | (0 if os.name == 'nt' else os.O_NOFOLLOW), 0o600)
        try:
            if os.name != 'nt' and os.geteuid() == 0:
                owner = self.root.stat()
                os.fchown(fd, owner.st_uid, owner.st_gid)
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ChatGPTAuthError('Credential files must not be symlinks')
        with self.path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ChatGPTAuthError('Invalid ChatGPT credential record')
        return data

    @staticmethod
    def _write(path, data):
        from ..agent_config import atomic_write
        if path.is_symlink():
            raise ChatGPTAuthError('Credential files must not be symlinks')
        owner = None
        if os.name != 'nt' and os.geteuid() == 0:
            owner = path.stat() if path.exists() else path.parent.stat()
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        if owner is not None:
            os.chown(path, owner.st_uid, owner.st_gid)
        os.chmod(path, 0o600)
        if os.name == "nt":
            from agent_harness._windows import private_file
            private_file(path)

    def _host(self):
        path = self.root / 'host.json'
        if path.is_symlink():
            raise ChatGPTAuthError('Host record must not be a symlink')
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data['ext_agent_host_id']
            if not isinstance(value, str) or not value.startswith('urn:uuid:'):
                raise ChatGPTAuthError('Invalid ChatGPT host ID')
            return value
        value = 'urn:uuid:' + str(uuid4())
        self._write(path, {'ext_agent_host_id': value})
        return value

    def status(self):
        with self._lock():
            data = self._read()
        return {'profile': self.profile, 'registered': bool(data.get('client_id')),
            'connected': bool(data.get('access_token')),
            'plan_usage': PLAN_SCOPE in data.get('scopes', []),
            'email': data.get('email'), 'expires_at': data.get('expires_at'),
            'credential_file': str(self.path), 'manage_usage': 'https://chatgpt.com/settings/usage'}

    def _activate(self, response, previous, client_id, nonce=None):
        if not isinstance(response, dict) or not isinstance(response.get('id_token'), str):
            raise ChatGPTAuthError('Token response did not include an ID token')
        if nonce is not None and not isinstance(response.get('scope'), str):
            raise ChatGPTAuthError('Code exchange did not report granted scopes')
        identity = _validate_identity(response['id_token'], client_id, nonce)
        if previous.get('subject') and identity['sub'] != previous['subject']:
            raise ChatGPTAuthError('Signed-in identity differs from this profile; use a separate profile')
        return self._tokens(response, {**previous, 'client_id': client_id,
            'subject': identity['sub'], 'issuer': identity['iss'], 'email': identity.get('email')})

    @staticmethod
    def _tokens(response, previous):
        if (not isinstance(response.get('access_token'), str) or not response['access_token']
                or response.get('token_type', '').lower() != 'bearer'
                or type(response.get('expires_in')) not in (int, float)
                or not math.isfinite(response['expires_in']) or response['expires_in'] <= 0):
            raise ChatGPTAuthError('Invalid token response')
        scopes = response.get('scope')
        if scopes is None:
            scopes = previous.get('scopes', [])
        elif isinstance(scopes, str):
            scopes = scopes.split()
        else:
            raise ChatGPTAuthError('Invalid granted scopes')
        if PLAN_SCOPE not in scopes:
            raise ChatGPTAuthError('ChatGPT plan usage was not authorized; enable it when signing in')
        now = time.time()
        data = {**previous, 'access_token': response['access_token'],
            'token_type': 'Bearer', 'scopes': scopes, 'saved_at': now,
            'expires_at': now + response['expires_in']}
        for key in ('refresh_token', 'id_token', 'earliest_refresh_at'):
            if key in response:
                if key in ('refresh_token', 'id_token') and (not isinstance(response[key], str) or not response[key]):
                    raise ChatGPTAuthError('Invalid credential in token response')
                data[key] = response[key]
        return data

    def login(self, *, port=54321, timeout=600, open_browser=True, announce=print):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ChatGPTAuthError('Invalid callback port')
        if type(timeout) is not int or not 1 <= timeout <= 3600:
            raise ChatGPTAuthError('Login timeout must be 1-3600 seconds')
        with self._lock():
            previous = self._read()
            host = self._host()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        selected = previous.get('client_id')
        callback = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Query includes the authorization code; never log it.

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                valid = (parsed.path == '/auth/callback' and all(len(v) == 1 for v in query.values())
                    and hmac.compare_digest(query.get('state', [''])[0], state))
                if not valid:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'Invalid callback. Keep the original sign-in window open.')
                    return
                callback.update({key: value[0] for key, value in query.items()})
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'Sign-in callback received. Return to Aster for verification.')

        with HTTPServer(('127.0.0.1', port), Handler) as server:
            server.timeout = 0.5
            redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
            parameters = {'client_id': selected or 'dynamic_agent_client', 'ext_agent_host_id': host,
                'response_type': 'code', 'redirect_uri': redirect, 'scope': SCOPES, 'resource': RESOURCE,
                'state': state, 'nonce': nonce, 'code_challenge_method': 'S256', 'code_challenge': challenge}
            if not selected:
                parameters['agent_name_hint'] = 'Aster'
            # Omit id_token_hint from printable CLI URLs so tokens never enter logs/chat.
            url = AUTHORIZE + '?' + urlencode(parameters)
            announce('Continue with ChatGPT — open this URL in a browser on this computer:\n' + url)
            announce('Waiting for the loopback callback. Credentials will not be printed.')
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + timeout
            while not callback and time.monotonic() < deadline:
                server.handle_request()
        if not callback:
            raise ChatGPTAuthError('Sign-in timed out; run login again')
        if callback.get('error'):
            raise ChatGPTAuthError('ChatGPT authorization was declined or could not be completed')
        client_id = callback.get('client_id', selected)
        if (not isinstance(client_id, str) or not client_id or client_id == 'dynamic_agent_client'
                or selected and selected != client_id or not callback.get('code')):
            raise ChatGPTAuthError('Missing or unexpected issued client ID in callback')
        # Retain the issued registration before token exchange, even if code expires.
        with self._lock():
            latest = self._read()
            if latest.get('client_id') not in (None, client_id):
                raise ChatGPTAuthError('Profile changed during sign-in; run login again')
            registration = {**latest, 'client_id': client_id, 'ext_agent_host_id': host}
            self._write(self.path, registration)
            response = _request_json(TOKEN, form={'grant_type': 'authorization_code',
                'client_id': client_id, 'code': callback['code'], 'code_verifier': verifier,
                'redirect_uri': redirect, 'resource': RESOURCE})
            active = self._activate(response, registration, client_id, nonce)
            self._write(self.path, active)
        return self.status()

    def access_token(self):
        # Serialize rotating refresh tokens across processes; reread under the lock.
        with self._lock():
            data = self._read()
            if '_pending_refresh' in data:
                try:
                    data = self._finish_refresh(data)
                except _ExpiredIdentity:
                    # Renew using the privately retained rotated credential, not
                    # the old refresh token. The expired identity is never used
                    # for access; the new response must pass full verification.
                    refresh = data['_pending_refresh']['response'].get('refresh_token')
                    if not isinstance(refresh, str) or not refresh:
                        raise ChatGPTAuthError(
                            'Pending identity expired without a retained refresh token; sign in again') from None
                    data = self._refresh(data, refresh)
            if not data.get('access_token') or PLAN_SCOPE not in data.get('scopes', []):
                raise ChatGPTAuthError('Run aster auth login and authorize ChatGPT plan usage first')
            if data.get('expires_at', 0) > time.time() + 60:
                return data['access_token']
            if not data.get('refresh_token'):
                raise ChatGPTAuthError('ChatGPT credentials expired; run aster auth login again')
            earliest = data.get('earliest_refresh_at', 0)
            if isinstance(earliest, (int, float)) and earliest > time.time():
                if data.get('expires_at', 0) > time.time():
                    return data['access_token']
                raise ChatGPTAuthError('Renewal is not yet available; sign in again')
            return self._refresh(data, data['refresh_token'])['access_token']

    def _refresh(self, data, refresh_token):
        # Called under the account lock; at most one exchange, without a retry loop.
        response = _request_json(TOKEN, form={'grant_type': 'refresh_token',
            'client_id': data['client_id'], 'refresh_token': refresh_token, 'resource': RESOURCE})
        # Retain rotation before validation. A previously quarantined identity
        # requires a fresh verified identity, even if the new response omits it.
        if 'id_token' in response or '_pending_refresh' in data:
            data = {**data, '_pending_refresh': {'response': response, 'received_at': time.time()}}
            self._write(self.path, data)
            return self._finish_refresh(data)
        renewed = self._tokens(response, data)
        self._write(self.path, renewed)
        return renewed

    def _finish_refresh(self, data):
        pending = data['_pending_refresh']
        renewed = self._activate(pending['response'], data, data['client_id'])
        renewed['expires_at'] = pending['received_at'] + pending['response']['expires_in']
        renewed.pop('_pending_refresh', None)
        self._write(self.path, renewed)
        return renewed

    def models(self):
        body = _request_json(RESOURCE + '/models', token=self.access_token())
        models = body.get('models', [])
        if not isinstance(models, list):
            raise ChatGPTAuthError('Invalid account model catalog')
        return [{'slug': m['slug'], 'display_name': m.get('display_name', m['slug'])}
                for m in models if isinstance(m, dict) and m.get('visibility') == 'list'
                and isinstance(m.get('slug'), str)]

    def logout(self):
        confirmed = True
        with self._lock():
            data = self._read()
            refresh_token = data.get('_pending_refresh', {}).get('response', {}).get('refresh_token') or data.get('refresh_token')
            if refresh_token:
                try:
                    _request_json(REVOKE, form={'token': refresh_token,
                        'token_type_hint': 'refresh_token', 'client_id': data['client_id']})
                except ChatGPTAuthError:
                    confirmed = False
            retained = {k: v for k, v in data.items() if k in
                        ('client_id', 'subject', 'issuer', 'email', 'ext_agent_host_id')}
            self._write(self.path, retained)
        return {'signed_out': True, 'remote_revocation_confirmed': confirmed,
                'manage_usage': 'https://chatgpt.com/settings/usage'}
