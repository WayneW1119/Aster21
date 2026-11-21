import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_harness.config import load_env_file
from agent_harness.core.client import ResponsesClient


class ConfigTests(unittest.TestCase):
    def test_dotenv_quotes_comments_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('HARNESS_MODEL="test-model" # comment\n'
                            "OPENAI_API_KEY='fake#key'\n"
                            "HARNESS_RESPONSES_URL=https://example.invalid/v1/responses\n")
            os.environ["HARNESS_MODEL"] = "override"
            load_env_file(path)
            self.assertEqual(os.environ["HARNESS_MODEL"], "override")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "fake#key")

    def test_parse_errors_do_not_expose_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('OPENAI_API_KEY="private-value\n')
            with self.assertRaises(ValueError) as error:
                load_env_file(path)
            self.assertNotIn("private-value", str(error.exception))

    def test_http_requires_explicit_opt_in(self):
        url = "http://example.invalid/v1/responses"
        with self.assertRaises(ValueError):
            ResponsesClient(url, "test", "fake")
        ResponsesClient(url, "test", "fake", allow_http=True)

    def test_reject_base_url_and_embedded_credentials(self):
        for url in ["https://example.invalid/v1", "https://user:password@host/v1/responses"]:
            with self.assertRaises(ValueError):
                ResponsesClient(url, "test", "fake")
