"""Provider-facing schema requirements, distinct from local argument routing."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from agent_harness.core.client import ResponsesClient, ScriptedClient
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager


def check_strict_objects(test, schema, path):
    if not isinstance(schema, dict):
        return
    if schema.get('type') == 'object':
        test.assertIs(schema.get('additionalProperties'), False, path)
        test.assertEqual(set(schema.get('required', [])), set(schema.get('properties', {})), path)
    for key, value in schema.items():
        if isinstance(value, dict):
            check_strict_objects(test, value, path + '.' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check_strict_objects(test, child, f'{path}.{key}[{index}]')


class ToolSchemaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.session = ThreadManager(root / 'threads').create(ScriptedClient([]), root,
            configuration=SessionConfiguration(code_mode_enabled=True))
        self.addAsyncCleanup(self.session.close)

    async def test_all_advertised_strict_functions_have_closed_fully_required_objects(self):
        count = 0
        for definition in self.session.router.registry.all_definitions():
            spec = definition['spec']
            if spec.get('type') != 'function':
                continue
            Draft202012Validator.check_schema(spec['parameters'])
            if spec.get('strict') is True:
                count += 1
                check_strict_objects(self, spec['parameters'], f"{definition['namespace']}.{spec['name']}")
        self.assertGreater(count, 5)

    async def test_actual_http_payload_preserves_explicit_non_strict_optional_send_parameter(self):
        prompt = StepContext.capture(self.session).prompt
        body = io.BytesIO(json.dumps({'status':'completed','output':[]}).encode())
        with patch('agent_harness.core.client.urlopen', return_value=body) as transport:
            ResponsesClient('https://example.test/responses', 'fixture', 'not-a-real-key', stream=False).respond(prompt)
        payload = json.loads(transport.call_args.args[0].data)
        send = next(spec for spec in payload['tools'] if spec.get('name') == 'send_user_message')
        self.assertIs(send['strict'], False)
        validator = Draft202012Validator(send['parameters'])
        validator.validate({'text':'hello'})
        validator.validate({'text':'hello','wake_model':False})
        self.assertFalse(validator.is_valid({'text':'hello','wake_model':None}))
        self.assertFalse(validator.is_valid({'text':'hello','other':True}))
        result = self.session.router.invoke({'type':'function_call','name':'send_user_message',
            'arguments':json.dumps({'text':'hello'})})
        self.assertTrue(result.wake_model)
        quiet = self.session.router.invoke({'type':'function_call','name':'send_user_message',
            'arguments':json.dumps({'text':'hello','wake_model':False})})
        self.assertFalse(quiet.wake_model)
