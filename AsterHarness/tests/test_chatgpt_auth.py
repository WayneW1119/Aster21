"""OAuth validation, protected storage and plan transport without real credentials."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen
from concurrent.futures import ThreadPoolExecutor
import threading

from agent_harness.auth.chatgpt import (ChatGPTAccount, ChatGPTAuthError,
    ChatGPTTransientAuthError, PLAN_SCOPE, _validate_identity, _request_json, _ExpiredIdentity)
from agent_harness.core.chatgpt_client import ChatGPTResponsesClient, project_plan_prompt
from agent_harness.core.client_common import ModelResponse, Prompt
from agent_harness.core.mcp.search import SEARCH_SPEC


def token_response(**overrides):
    return {'access_token': 'access-sentinel', 'refresh_token': 'refresh-sentinel',
            'id_token': 'id-sentinel', 'token_type': 'Bearer', 'expires_in': 3600,
            'scope': 'openid offline_access ' + PLAN_SCOPE, **overrides}


class ChatGPTAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.account = ChatGPTAccount(self.home)

    def seed(self, expires=3600):
        data = {'client_id': 'issued-client', 'subject': 'same-person',
            'access_token': 'old-access', 'refresh_token': 'old-refresh',
            'scopes': [PLAN_SCOPE], 'expires_at': time.time() + expires}
        self.account.root.mkdir(parents=True)
        self.account._write(self.account.path, data)
        return data

    @unittest.skipUnless(hasattr(os, 'geteuid'), 'POSIX ownership required')
    def test_root_writer_retains_host_credential_and_lock_ownership(self):
        self.seed()
        owner = self.account.root.stat()
        with patch('os.geteuid', return_value=0), patch('os.chown') as chown, patch('os.fchown') as fchown:
            self.account._write(self.account.path, token_response())
            self.account._write(self.account.root/'pending-test.json', {'response': 'private'})
            with self.account._lock():
                pass
        self.assertEqual(chown.call_count, 2)
        for call in chown.call_args_list:
            self.assertEqual(call.args[1:], (owner.st_uid, owner.st_gid))
        self.assertEqual(fchown.call_args.args[1:], (owner.st_uid, owner.st_gid))

    def test_login_real_loopback_callback_pkce_and_registration(self):
        messages = []
        def announce(text):
            messages.append(text)
            if text.startswith('Continue'):
                url = text.split('\n', 1)[1]
                query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
                self.assertEqual(query['client_id'], 'dynamic_agent_client')
                self.assertEqual(query['agent_name_hint'], 'Aster')
                self.assertEqual(query['code_challenge_method'], 'S256')
                self.assertIn(PLAN_SCOPE, query['scope'])
                callback_url = query['redirect_uri'] + '?' + urlencode({
                    'code': 'test-code', 'state': query['state'], 'client_id': 'issued-client'})
                threading.Thread(target=lambda: urlopen(callback_url, timeout=5).read(), daemon=True).start()
        with patch('agent_harness.auth.chatgpt._request_json', return_value=token_response()) as exchange, \
             patch('agent_harness.auth.chatgpt._validate_identity', return_value={'sub': 'same-person',
                   'iss': 'https://auth.openai.com', 'email': 'test@example.com'}):
            status = self.account.login(port=0, timeout=5, open_browser=False, announce=announce)
        self.assertTrue(status['connected'])
        self.assertTrue(status['plan_usage'])
        form = exchange.call_args.kwargs['form']
        self.assertEqual(form['client_id'], 'issued-client')
        self.assertIn('code_verifier', form)
        self.assertNotIn('access-sentinel', json.dumps(status) + ''.join(messages))
        if os.name != 'nt':
            self.assertEqual(self.account.path.stat().st_mode & 0o777, 0o600)
        first_host = self.account._host()
        self.assertEqual(first_host, ChatGPTAccount(self.home)._host())

    def test_reject_unsigned_wrong_nonce_and_wrong_audience_identity(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest('chatgpt extra not installed')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'sub': 'person', 'iss': 'https://auth.openai.com', 'aud': 'issued',
                  'exp': int(time.time()) + 300, 'nonce': 'correct'}
        encoded = jwt.encode(claims, key, algorithm='RS256')
        signing = type('SigningKey', (), {'key': key.public_key()})()
        with patch('jwt.PyJWKClient') as client:
            client.return_value.get_signing_key_from_jwt.return_value = signing
            self.assertEqual(_validate_identity(encoded, 'issued', 'correct')['sub'], 'person')
            for audience, nonce in [('wrong', 'correct'), ('issued', 'wrong')]:
                with self.assertRaises(ChatGPTAuthError):
                    _validate_identity(encoded, audience, nonce)
            unsigned = jwt.encode(claims, '', algorithm='none')
            with self.assertRaises(ChatGPTAuthError):
                _validate_identity(unsigned, 'issued', 'correct')

    def test_plan_permission_and_identity_mismatch_never_replace_tokens(self):
        before = self.seed()
        with patch('agent_harness.auth.chatgpt._validate_identity', return_value={
            'sub': 'other', 'iss': 'https://auth.openai.com'}):
            with self.assertRaises(ChatGPTAuthError):
                self.account._activate(token_response(), before, 'issued-client', 'nonce')
        with self.assertRaises(ChatGPTAuthError):
            self.account._tokens(token_response(scope='openid'), before)
        self.assertEqual(self.account._read(), before)

    def test_rotating_refresh_serializes_and_reuses_new_token(self):
        self.seed(expires=-1)
        response = token_response()
        response.pop('id_token')
        with patch('agent_harness.auth.chatgpt._request_json', return_value=response) as refresh:
            with ThreadPoolExecutor(2) as pool:
                values = list(pool.map(lambda _: self.account.access_token(), range(2)))
        self.assertEqual(values, ['access-sentinel'] * 2)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.account._read()['refresh_token'], 'refresh-sentinel')
        self.assertNotIn('scope', refresh.call_args.kwargs['form'])

    def test_identity_allows_only_small_clock_skew_and_keeps_other_checks(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest('chatgpt extra not installed')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'sub': 'same-person', 'iss': 'https://auth.openai.com',
                  'aud': 'issued-client', 'iat': int(time.time()) + 2,
                  'nbf': int(time.time()) + 2, 'exp': int(time.time()) + 3600}
        signing = type('SigningKey', (), {'key': key.public_key()})()
        with patch('jwt.PyJWKClient') as client:
            client.return_value.get_signing_key_from_jwt.return_value = signing
            self.assertEqual(_validate_identity(jwt.encode(claims, key, algorithm='RS256'),
                                                'issued-client')['sub'], 'same-person')
            for changes in ({'iat': int(time.time()) + 60}, {'nbf': int(time.time()) + 60},
                            {'exp': int(time.time()) - 60}, {'aud': 'other'}, {'iss': 'other'}):
                encoded = jwt.encode({**claims, **changes}, key, algorithm='RS256')
                with self.subTest(changes=changes), self.assertRaises(ChatGPTAuthError):
                    _validate_identity(encoded, 'issued-client')

    def pending_expired_identity(self):
        data = self.seed(expires=-1)
        data['_pending_refresh'] = {'response': token_response(refresh_token='rotated-once'),
                                   'received_at': time.time() - 7200}
        self.account._write(self.account.path, data)
        return data

    def test_expired_pending_identity_renews_rotated_token_once_across_callers(self):
        self.pending_expired_identity()
        def validate(encoded, *args):
            if encoded == 'id-sentinel':
                raise _ExpiredIdentity('expired')
            return {'sub': 'same-person', 'iss': 'https://auth.openai.com'}
        with patch('agent_harness.auth.chatgpt._validate_identity', side_effect=validate), \
             patch('agent_harness.auth.chatgpt._request_json', return_value=token_response(
                 id_token='fresh-identity', refresh_token='rotated-twice')) as refresh:
            with ThreadPoolExecutor(4) as pool:
                values = list(pool.map(lambda _: self.account.access_token(), range(4)))
        self.assertEqual(values, ['access-sentinel'] * 4)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(refresh.call_args.kwargs['form']['refresh_token'], 'rotated-once')
        self.assertNotIn('_pending_refresh', self.account._read())
        self.assertEqual(self.account._read()['refresh_token'], 'rotated-twice')

    def test_expired_pending_identity_never_activates_unverified_replacement(self):
        self.pending_expired_identity()
        for replacement, error in [('bad-identity', ChatGPTAuthError('bad signature')),
                                   ('expired-again', _ExpiredIdentity('expired again'))]:
            with self.subTest(replacement=replacement):
                if replacement != 'bad-identity':
                    data = self.account._read()
                    data['_pending_refresh']['response']['id_token'] = 'id-sentinel'
                    self.account._write(self.account.path, data)
                with patch('agent_harness.auth.chatgpt._validate_identity',
                           side_effect=[_ExpiredIdentity('expired'), error]), \
                     patch('agent_harness.auth.chatgpt._request_json', return_value=token_response(
                         id_token=replacement, refresh_token='rotated-twice')) as refresh:
                    with self.assertRaises(ChatGPTAuthError):
                        self.account.access_token()
                saved = self.account._read()
                self.assertEqual(saved['access_token'], 'old-access')
                self.assertEqual(saved['_pending_refresh']['response']['refresh_token'], 'rotated-twice')
                self.assertEqual(refresh.call_count, 1)  # No recursive renew/retry.

    def test_expired_pending_identity_without_rotated_credential_requires_login(self):
        data = self.pending_expired_identity()
        del data['_pending_refresh']['response']['refresh_token']
        self.account._write(self.account.path, data)
        with patch('agent_harness.auth.chatgpt._validate_identity', side_effect=_ExpiredIdentity('expired')), \
             patch('agent_harness.auth.chatgpt._request_json') as refresh:
            with self.assertRaisesRegex(ChatGPTAuthError, 'sign in again'):
                self.account.access_token()
        refresh.assert_not_called()

    def test_expired_pending_identity_requires_fresh_identity_in_replacement(self):
        self.pending_expired_identity()
        response = token_response(refresh_token='rotated-twice')
        del response['id_token']
        with patch('agent_harness.auth.chatgpt._validate_identity', side_effect=_ExpiredIdentity('expired')), \
             patch('agent_harness.auth.chatgpt._request_json', return_value=response) as refresh:
            with self.assertRaisesRegex(ChatGPTAuthError, 'did not include an ID token'):
                self.account.access_token()
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.account._read()['access_token'], 'old-access')
        self.assertEqual(self.account._read()['_pending_refresh']['response']['refresh_token'], 'rotated-twice')

    def test_jwks_failure_preserves_rotated_response_without_activating_it(self):
        self.seed(expires=-1)
        with patch('agent_harness.auth.chatgpt._request_json', return_value=token_response()) as refresh, \
             patch('agent_harness.auth.chatgpt._validate_identity', side_effect=[
                 ChatGPTTransientAuthError('JWKS unavailable'),
                 {'sub': 'same-person', 'iss': 'https://auth.openai.com'}]):
            with self.assertRaises(ChatGPTTransientAuthError):
                self.account.access_token()
            pending = self.account._read()
            self.assertEqual(pending['access_token'], 'old-access')
            self.assertEqual(pending['refresh_token'], 'old-refresh')
            self.assertNotIn('access-sentinel', json.dumps(self.account.status()))
            if os.name != 'nt':
                self.assertEqual(self.account.path.stat().st_mode & 0o777, 0o600)
            # Also survives a new account object/process, with no second exchange.
            self.assertEqual(ChatGPTAccount(self.home).access_token(), 'access-sentinel')
        self.assertEqual(refresh.call_count, 1)
        self.assertNotIn('_pending_refresh', self.account._read())
        self.assertEqual(self.account._read()['refresh_token'], 'refresh-sentinel')

    def test_logout_revokes_pending_rotated_refresh_without_activation(self):
        self.seed(expires=-1)
        with patch('agent_harness.auth.chatgpt._request_json', return_value=token_response()), \
             patch('agent_harness.auth.chatgpt._validate_identity', side_effect=ChatGPTTransientAuthError('offline')):
            with self.assertRaises(ChatGPTTransientAuthError):
                self.account.access_token()
        with patch('agent_harness.auth.chatgpt._request_json', return_value={}) as revoke:
            self.account.logout()
            self.assertEqual(revoke.call_args.kwargs['form']['token'], 'refresh-sentinel')
        self.assertNotIn('_pending_refresh', self.account._read())

    def test_pending_invalid_identity_cannot_activate_credentials(self):
        self.seed(expires=-1)
        with patch('agent_harness.auth.chatgpt._request_json', return_value=token_response()) as refresh, \
             patch('agent_harness.auth.chatgpt._validate_identity', side_effect=ChatGPTAuthError('bad signature')):
            for _ in range(2):
                with self.assertRaises(ChatGPTAuthError):
                    self.account.access_token()
                self.assertEqual(self.account._read()['access_token'], 'old-access')
            self.assertEqual(refresh.call_count, 1)

    def test_jwks_connection_failure_is_retryable_but_invalid_identity_is_not(self):
        try:
            import jwt
        except ImportError:
            self.skipTest('chatgpt extra not installed')
        with patch('jwt.PyJWKClient') as client:
            client.return_value.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError('offline')
            with self.assertRaises(ChatGPTTransientAuthError):
                _validate_identity('opaque', 'issued-client')
            client.return_value.get_signing_key_from_jwt.side_effect = jwt.InvalidTokenError('invalid')
            with self.assertRaises(ChatGPTAuthError) as raised:
                _validate_identity('opaque', 'issued-client')
            self.assertNotIsInstance(raised.exception, ChatGPTTransientAuthError)

    def test_key_lookup_failures_retry_but_verified_token_failures_do_not(self):
        try:
            import jwt
        except ImportError:
            self.skipTest('chatgpt extra not installed')
        with patch('jwt.PyJWKClient') as client:
            for error in (jwt.PyJWKClientConnectionError('secret'),
                          jwt.PyJWKClientError('secret'),
                          jwt.PyJWKSetError('secret'),
                          json.JSONDecodeError('secret', '', 0), OSError('secret')):
                client.return_value.get_signing_key_from_jwt.side_effect = error
                with self.subTest(error=type(error).__name__), self.assertRaises(ChatGPTTransientAuthError) as caught:
                    _validate_identity('opaque', 'issued-client')
                self.assertNotIn('secret', str(caught.exception))
            client.return_value.get_signing_key_from_jwt.side_effect = None
            client.return_value.get_signing_key_from_jwt.return_value = type('Key', (), {'key': 'unused'})()
            for error in (jwt.InvalidSignatureError('secret'), jwt.ExpiredSignatureError('secret'),
                          jwt.InvalidAudienceError('secret'), jwt.InvalidIssuerError('secret')):
                with self.subTest(error=type(error).__name__), patch('jwt.decode', side_effect=error):
                    with self.assertRaises(ChatGPTAuthError) as caught:
                        _validate_identity('opaque', 'issued-client')
                    self.assertNotIsInstance(caught.exception, ChatGPTTransientAuthError)
                    self.assertIn(type(error).__name__, str(caught.exception))
                    self.assertNotIn('secret', str(caught.exception))

    def test_malformed_jwks_retains_rotation_until_real_signature_verification_succeeds(self):
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import rsa
        except ImportError:
            self.skipTest('chatgpt extra not installed')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'sub': 'same-person', 'iss': 'https://auth.openai.com',
                  'aud': 'issued-client', 'exp': int(time.time()) + 300}
        encoded = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'test-key'})
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        jwk['kid'] = 'test-key'
        # A valid HTTP/JSON response can still have an unusable signing-key set.
        for index, bad in enumerate(({'keys': []}, {'error': 'secret upstream failure'})):
            with self.subTest(body=bad):
                self.account = ChatGPTAccount(self.home / str(index))
                self.seed(expires=-1)
                with patch('agent_harness.auth.chatgpt._request_json',
                           return_value=token_response(id_token=encoded)) as refresh, \
                     patch('urllib.request.urlopen', side_effect=[
                         io.BytesIO(json.dumps(bad).encode()),
                         io.BytesIO(json.dumps({'keys': [jwk]}).encode())]) as keys, \
                     patch('urllib.request.build_opener') as opener:
                    # PyJWT versions use either urlopen or a redirect-safe opener.
                    # Keep both transports offline while exercising real JWKS parsing.
                    opener.return_value.open = keys
                    with self.assertRaises(ChatGPTTransientAuthError) as caught:
                        self.account.access_token()
                    self.assertNotIn('secret', str(caught.exception))
                    self.assertEqual(self.account._read()['access_token'], 'old-access')
                    self.assertIn('_pending_refresh', self.account._read())
                    self.assertEqual(self.account.access_token(), 'access-sentinel')
                    self.assertNotIn('_pending_refresh', self.account._read())
                self.assertEqual(refresh.call_count, 1)
                self.assertEqual(keys.call_count, 2)

    def test_auth_http_transient_and_permanent_errors_are_distinct_and_redacted(self):
        from urllib.error import HTTPError, URLError
        for error, retryable in [(HTTPError('https://auth.openai.com', 503, 'secret', {}, None), True),
                                 (HTTPError('https://auth.openai.com', 429, 'secret', {}, None), True),
                                 (URLError('secret'), True),
                                 (HTTPError('https://auth.openai.com', 401, 'secret', {}, None), False)]:
            with self.subTest(error=type(error).__name__, retryable=retryable), \
                 patch('agent_harness.auth.chatgpt.urlopen', side_effect=error):
                with self.assertRaises(ChatGPTAuthError) as raised:
                    _request_json('https://auth.openai.com/test')
                self.assertEqual(isinstance(raised.exception, ChatGPTTransientAuthError), retryable)
                self.assertNotIn('secret', str(raised.exception))

    def test_failed_refresh_preserves_credentials_and_never_falls_back_to_api_key(self):
        before = self.seed(expires=-1)
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'unrelated-key'}), \
             patch('agent_harness.auth.chatgpt._request_json', side_effect=ChatGPTAuthError('failed')):
            with self.assertRaises(ChatGPTAuthError):
                self.account.access_token()
        self.assertEqual(self.account._read(), before)

    def test_status_redacted_and_logout_retains_registration(self):
        self.seed()
        status = json.dumps(self.account.status())
        self.assertNotIn('old-access', status)
        self.assertNotIn('old-refresh', status)
        with patch('agent_harness.auth.chatgpt._request_json', return_value={}):
            result = self.account.logout()
        self.assertTrue(result['remote_revocation_confirmed'])
        self.assertEqual(self.account._read()['client_id'], 'issued-client')
        self.assertNotIn('access_token', self.account._read())
        with self.assertRaises(ChatGPTAuthError):
            self.account.access_token()

    def test_native_search_is_wire_function_and_definitions_are_additional_tools(self):
        definition = {'type': 'namespace', 'name': 'remote', 'description': 'Remote tools',
            'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}, 'defer_loading': True}]}
        prompt = Prompt('Keep Aster rules.', [
            {'type': 'tool_search_call', 'call_id': 'search', 'arguments': {'query': 'remote'}},
            {'type': 'tool_search_output', 'call_id': 'search', 'tools': [definition]}],
            [deepcopy(SEARCH_SPEC), {'type': 'function', 'name': 'wait_for_event',
                                     'parameters': {'type': 'object'}}])
        before = deepcopy(prompt)
        wire = project_plan_prompt(prompt)
        self.assertEqual(prompt, before)
        self.assertTrue(all(tool['type'] == 'namespace' for tool in wire.tools))
        self.assertEqual(wire.input[0]['type'], 'function_call')
        self.assertEqual(wire.input[1]['type'], 'function_call_output')
        self.assertEqual(wire.input[2]['type'], 'additional_tools')
        self.assertEqual(wire.input[2]['role'], 'developer')
        self.assertNotIn('defer_loading', wire.input[2]['tools'][0]['tools'][0])
        compacted = project_plan_prompt(Prompt('rules', [], prompt.tools))
        self.assertEqual(compacted.input, [])

    def test_foreign_history_projection_preserves_calls_and_visible_reasoning(self):
        prompt = Prompt('rules', [
            {'type': 'reasoning', 'id': 'c744db00-0000-4000-8000-000000000001',
             'status': 'completed', 'encrypted_content': 'foreign-opaque',
             'summary': [], 'content': [{'type': 'reasoning_text', 'text': 'Verified progress.'}]},
            {'type': 'function_call', 'id': 'foreign-call-id', 'status': 'completed',
             'name': 'read_file', 'call_id': 'call-1', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call-1', 'output': 'data'},
            {'type': 'reasoning', 'id': 'rs_native', 'status': 'completed',
             'summary': [], 'encrypted_content': 'native-opaque'},
        ], [])
        before = deepcopy(prompt)
        wire = project_plan_prompt(prompt)
        self.assertEqual(prompt, before)
        self.assertEqual(wire.input[0]['role'], 'assistant')
        self.assertIn('Verified progress.', wire.input[0]['text'])
        self.assertNotIn('foreign-opaque', json.dumps(wire.input))
        self.assertNotIn('status', wire.input[1])
        self.assertNotIn('id', wire.input[1])
        self.assertEqual(wire.input[1]['call_id'], wire.input[2]['call_id'])
        self.assertEqual(wire.input[3]['encrypted_content'], 'native-opaque')
        self.assertNotIn('status', wire.input[3])

    def test_qualified_historical_tool_names_are_split_without_replaying_calls(self):
        for kind in ('function_call', 'custom_tool_call'):
            with self.subTest(kind=kind):
                call = {'type': kind, 'id': 'foreign-call',
                        'name': 'mcp__relay::relay_receive', 'call_id': 'same-call'}
                call['arguments' if kind == 'function_call' else 'input'] = '{}'
                receipt = {'type': 'function_call_output' if kind == 'function_call' else 'custom_tool_call_output',
                           'call_id': 'same-call', 'output': 'Unknown tool: mcp__relay::relay_receive'}
                prompt = Prompt('rules', [call, receipt], [])
                before = deepcopy(prompt)
                wire = project_plan_prompt(prompt)
                self.assertEqual(prompt, before)
                self.assertEqual(wire.input[0]['name'], 'relay_receive')
                self.assertEqual(wire.input[0]['namespace'], 'mcp__relay')
                self.assertEqual(wire.input[0]['call_id'], receipt['call_id'])
                self.assertEqual(wire.input[1], receipt)
                self.assertEqual(project_plan_prompt(wire), wire)

    def test_foreign_message_ids_are_projected_without_changing_history(self):
        prompt = Prompt('rules', [
            {'type': 'message', 'role': 'assistant',
             'id': 'ee85626f-019c-4e20-a3eb-9f9bc566901d',
             'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Prior result.'}]},
            {'type': 'message', 'role': 'assistant', 'id': 'foreign-opaque-message',
             'text': 'Another provider result.'},
            {'type': 'message', 'role': 'assistant', 'id': 'msg_native', 'text': 'Native result.'},
            {'type': 'message', 'role': 'user', 'text': 'Continue.'},
        ], [], cache_session_id='same-thread')
        before = deepcopy(prompt)
        wire = project_plan_prompt(prompt)
        self.assertEqual(prompt, before)
        for index in (0, 1):
            expected = deepcopy(prompt.input[index])
            expected.pop('id')
            expected.pop('status', None)
            self.assertEqual(wire.input[index], expected)
        self.assertEqual(wire.input[2:], prompt.input[2:])
        self.assertEqual(wire.cache_session_id, 'same-thread')

    def test_transport_retrieves_fresh_oauth_and_restores_native_search_calls(self):
        response = ModelResponse([{'type': 'function_call', 'call_id': 'search', 'name': 'tool_search',
            'namespace': 'functions', 'arguments': '{"query":"hello"}'}], {'input_tokens': 5})
        client = ChatGPTResponsesClient(self.home, 'chosen-model')
        with patch.object(ChatGPTAccount, 'access_token', return_value='private-credential'), \
             patch('agent_harness.core.chatgpt_client.ResponsesClient') as transport:
            transport.return_value.respond.return_value = response
            result = client.respond(Prompt('rules', [], [deepcopy(SEARCH_SPEC)]))
        self.assertEqual(result.output[0]['type'], 'tool_search_call')
        self.assertEqual(result.output[0]['arguments'], {'query': 'hello'})
        self.assertEqual(transport.call_args.args[0], 'https://api.openai.com/v1/responses')
        self.assertTrue(transport.call_args.kwargs['stream'])
        self.assertTrue(transport.call_args.kwargs['allow_empty_output'])

    def test_profile_path_escape_and_nonfinite_lifetime_rejected(self):
        for name in ('../outside', '', '/tmp/path'):
            with self.assertRaises(ChatGPTAuthError):
                ChatGPTAccount(self.home, name)
        for lifetime in (float('nan'), float('inf'), -1, True):
            with self.assertRaises(ChatGPTAuthError):
                self.account._tokens(token_response(expires_in=lifetime), {})

    def test_profile_and_sdk_auth_settings_pin_credentials_outside_sandbox(self):
        from agent_harness.agent_config import load_profile, toml_text
        from agent_harness.profiles import client_for, private_paths
        if os.name == 'nt' and not os.environ.get('ASTER_MXC_EXECUTOR'):
            executor = os.environ.get('ASTER_MXC_TEST_EXECUTOR')
            if not executor:
                self.skipTest('Windows MXC executor required')
            self.enterContext(patch.dict(os.environ, {'ASTER_MXC_EXECUTOR': executor}))
        config = self.home / 'agent.toml'
        (self.home / 'instructions.md').write_text('Test assistant.')
        data = {'name': 'test', 'workspace': str(self.home), 'model': {'name': 'available-model',
            'auth': 'chatgpt', 'auth_home': str(self.home / 'auth-store')}}
        config.write_text(toml_text(data))
        profile = load_profile(config)
        self.assertIsInstance(client_for(profile, lambda *_: None), ChatGPTResponsesClient)
        self.assertIn(self.home / 'auth-store/credentials', private_paths(self.home, profile))
        self.assertEqual(profile.snapshot()['model']['auth'], 'chatgpt')
        data['model']['url'] = 'https://other.example/responses'
        config.write_text(toml_text(data))
        with self.assertRaisesRegex(ValueError, 'official'):
            load_profile(config)


class PlanSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_sse_search_deferred_call_and_message_use_original_event_loop(self):
        import asyncio
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from agent_harness.core.client import ResponsesClient
        from agent_harness.core.session import Session
        from agent_harness.core.tools.handlers import build_file_tools
        from agent_harness.core.tools.registry import Tool
        requests, messages = [], []
        responses = [
            [{'type': 'function_call', 'call_id': 'search', 'namespace': 'functions',
              'name': 'tool_search', 'arguments': '{"query":"sample lookup"}'}],
            [{'type': 'function_call', 'call_id': 'lookup', 'namespace': 'sample',
              'name': 'lookup', 'arguments': '{}'}],
            [{'type': 'function_call', 'call_id': 'send', 'namespace': 'functions',
              'name': 'send_user_message', 'arguments': '{"text":"Lookup verified.","wake_model":false}'}]]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(body)
                output = responses[len(requests)-1]
                data = {'type': 'response.completed', 'response': {'status': 'completed',
                    'output': output, 'usage': {'input_tokens': 100}}}
                self.send_response(200)
                # The real plan endpoint can omit Content-Type despite sending SSE.
                self.end_headers()
                # Reproduce the direct route: complete items, empty terminal array.
                for index, item in enumerate(output):
                    event = {'type': 'response.output_item.done', 'output_index': index, 'item': item}
                    self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
                data['response']['output'] = []
                self.wfile.write(('data: '+json.dumps(data)+'\n\n').encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            registry = build_file_tools(root)
            registry.register(Tool({'type': 'function', 'name': 'lookup', 'description': 'Sample lookup',
                'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
                lambda: 'VERIFIED_LOOKUP', deferred=True), namespace='sample', namespace_description='Sample lookup')
            def transport(endpoint, model, token, **kwargs):
                return ResponsesClient(f'http://127.0.0.1:{server.server_port}/responses', model,
                                       token, allow_http=True, **kwargs)
            client = ChatGPTResponsesClient(root, 'model')
            session = Session(client, registry, workspace=root, send_message=messages.append)
            try:
                with patch.object(ChatGPTAccount, 'access_token', return_value='local-sentinel'), \
                     patch('agent_harness.core.chatgpt_client.ResponsesClient', side_effect=transport):
                    await session.post_user_message('Search sample lookup, call it and report.')
                    await session.run_until_idle()
                self.assertEqual(messages, ['Lookup verified.'])
                self.assertEqual(len(requests), 3)
                self.assertTrue(all(r['stream'] and r['store'] is False for r in requests))
                self.assertTrue(all(t['type'] == 'namespace' for r in requests for t in r['tools']))
                self.assertTrue(any(i['type'] == 'additional_tools' for i in requests[1]['input']))
                self.assertIn('VERIFIED_LOOKUP', json.dumps(requests[2]['input']))
                self.assertFalse(any(i['type'].startswith('tool_search') for r in requests for i in r['input']))
                self.assertTrue(any(i['type'] == 'tool_search_output' for i in session.state.history.snapshot()))
            finally:
                await session.close()
                await asyncio.to_thread(server.shutdown)
                server.server_close()
                thread.join()
