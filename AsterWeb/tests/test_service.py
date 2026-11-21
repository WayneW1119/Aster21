import json
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer

from aster_web.service import Service, RetrievalError, public_url, exa_results


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_mandatory_and_invalid_input_never_calls_network(self):
        service = Service()
        service._exa = AsyncMock()
        service._deepseek = AsyncMock()
        for args in (("q", "auto", 3), ("", "exa", 3), ("x" * 129, "deepseek", 3), ("q", "exa", True)):
            with self.subTest(args=args), self.assertRaises(RetrievalError):
                await service.search(*args)
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://user:password@example.com", "http://localhost", "http://169.254.169.254"):
            with self.subTest(url=url), self.assertRaises(RetrievalError):
                await service.fetch(url, "exa")
        service._exa.assert_not_awaited(); service._deepseek.assert_not_awaited()
        self.assertEqual(public_url("https://example.com/a"), "https://example.com/a")

    async def test_search_normalization_empty_results_and_output_budget(self):
        service = Service()
        service._deepseek = AsyncMock(return_value={"results": [
            {"title": "Reference", "url": "https://example.com", "summary": "x" * 1600, "date": None}]})
        result = await service.search(" question ", "deepseek", 3)
        self.assertEqual(result["provider"], "deepseek")
        self.assertEqual(result["query"], "question")
        self.assertEqual(len(result["results"][0]["summary"]), 1500)
        self.assertNotIn("published_at", result["results"][0]); self.assertTrue(result["truncated"])
        service._deepseek.return_value = {"results": []}
        self.assertEqual((await service.search("q", "deepseek"))["results"], [])
        service._deepseek.return_value = {"error": "quota"}
        with self.assertRaises(RetrievalError): await service.search("q", "deepseek")

    async def test_exa_text_adapter_preserves_sources_and_detects_format_drift(self):
        service = Service()
        service._exa = AsyncMock(return_value="Title: Docs\nURL: https://example.com/doc\nPublished: 2026-11-01\nAuthor: N/A\nHighlights:\nSource text")
        result = await service.search("q", "exa", 2)
        self.assertEqual(result["results"], [{"title": "Docs", "url": "https://example.com/doc", "summary": "Source text", "published_at": "2026-11-01"}])
        self.assertEqual(exa_results("No results found."), [])
        for raw in ("changed response", '{"error":"quota"}', "Title: Invalid\nURL: not-a-url\n"):
            with self.subTest(raw=raw), self.assertRaises(RetrievalError): exa_results(raw)

    async def test_fetch_limits_and_failure_does_not_switch_provider(self):
        service = Service()
        service._deepseek = AsyncMock(return_value={"title": "Page", "content": "a" * 200})
        service._exa = AsyncMock(return_value="# Page\nURL: https://example.com\n\n" + "a" * 200)
        for provider in ("exa", "deepseek"):
            result = await service.fetch("https://example.com", provider, 100)
            self.assertEqual(len(result["content"]), 100); self.assertTrue(result["truncated"])
        service._exa.reset_mock()
        service._deepseek.side_effect = RetrievalError("quota", "DeepSeek unavailable", status=402)
        with self.assertRaises(RetrievalError) as caught: await service.fetch("https://example.com", "deepseek")
        self.assertEqual(caught.exception.public("deepseek")["error"]["upstream_status"], 402)
        service._exa.assert_not_awaited()

    async def test_deepseek_http_errors_are_sanitized_and_not_retried(self):
        calls = []
        async def respond(request):
            calls.append((request.path, request.headers.get("Authorization"), await request.json()))
            if request.path == "/search": return web.json_response({"results": []})
            return web.json_response({"error": {"message": "secret-key-reflection"}}, status=429)
        app = web.Application(); app.router.add_post("/{action}", respond)
        server = TestServer(app); await server.start_server()
        self.addAsyncCleanup(server.close)
        service = Service(deepseek_key="test-key", deepseek_url=str(server.make_url("/")))
        self.assertEqual((await service.search("q", "deepseek"))["results"], [])
        self.assertEqual(calls[0][1], "Bearer test-key")
        self.assertFalse(calls[0][2]["need_content"])
        with self.assertRaises(RetrievalError) as caught: await service.fetch("https://example.com", "deepseek")
        self.assertEqual(len(calls), 2)
        self.assertTrue(caught.exception.retryable)
        self.assertNotIn("secret", json.dumps(caught.exception.public("deepseek")))

    async def test_missing_deepseek_credentials_is_explicit(self):
        with self.assertRaises(RetrievalError) as caught: await Service().search("q", "deepseek")
        self.assertEqual(caught.exception.code, "not_configured")
