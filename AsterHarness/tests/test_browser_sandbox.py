"""Opt-in real stdio/browser integration; no model credentials or external sites."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

AVAILABLE = (
    os.name == "posix"
    and importlib.util.find_spec("mcp") is not None
    and all(
        os.environ.get(key)
        for key in ("ASTER_TEST_NODE", "ASTER_TEST_BROWSER_MCP", "ASTER_TEST_CHROMIUM")
    )
)


@unittest.skipUnless(
    AVAILABLE, "Explicit Node, Playwright MCP and Chromium paths required"
)
class BrowserSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_browser_keeps_both_sandboxes(self):
        from agent_harness.core.mcp.client import create_mcp_client
        from agent_harness.core.mcp.config import McpServerConfig
        from agent_harness.core.sandbox import FilesystemSandbox

        with tempfile.TemporaryDirectory(prefix="aster-browser-test-") as directory:
            root = Path(directory)
            work = root / "work"
            work.mkdir()
            config = McpServerConfig(
                command="/usr/bin/env",
                args=(
                    "XDG_CACHE_HOME=/tmp/aster-browser/cache",
                    "XDG_CONFIG_HOME=/tmp/aster-browser/config",
                    os.environ["ASTER_TEST_NODE"],
                    os.environ["ASTER_TEST_BROWSER_MCP"],
                    "--executable-path",
                    os.environ["ASTER_TEST_CHROMIUM"],
                    "--headless",
                    "--isolated",
                    "--sandbox",
                    "--no-webmcp",
                    "--idle-timeout",
                    "60000",
                ),
                startup_timeout_sec=20,
                tool_timeout_sec=30,
            )
            client = create_mcp_client(config, work, root / "stderr.log")
            client.sandbox = FilesystemSandbox(
                work, {"rules": [{"path": "/", "access": "read"}], "local_ipc": True}
            )

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                self.assertFalse(result.get("isError"), result)
                return str(result)

            try:
                await client.start()
                await call(
                    "browser_navigate",
                    {
                        "url": "data:text/html,<title>Sandbox test</title><input id=q><p id=result></p>"
                    },
                )
                result = await call(
                    "browser_evaluate",
                    {
                        "function": "() => { document.querySelector('#q').value = 'hello'; document.querySelector('#result').textContent = document.querySelector('#q').value; return document.querySelector('#result').textContent; }"
                    },
                )
                self.assertIn("hello", result)
                await call("browser_take_screenshot", {"filename": "page.png"})
                screenshots = list(work.rglob("page.png"))
                self.assertEqual(len(screenshots), 1)
                self.assertTrue(
                    screenshots[0].read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                )
                await call("browser_navigate", {"url": "chrome://sandbox"})
                response = await client.call_tool(
                    "browser_evaluate",
                    {
                        "function": "() => Object.fromEntries(Array.from(document.querySelectorAll('tr')).map(r => Array.from(r.cells).map(c => c.textContent.trim())))"
                    },
                )
                self.assertFalse(response.get("isError"), response)
                text = next(
                    item["text"]
                    for item in response["content"]
                    if "### Result" in item.get("text", "")
                )
                status, _ = json.JSONDecoder().raw_decode(
                    text.split("### Result", 1)[1].lstrip()
                )
                self.assertEqual(status["Layer 1 Sandbox"], "Namespace")
                for label in (
                    "PID namespaces",
                    "Network namespaces",
                    "Seccomp-BPF sandbox",
                    "Seccomp-BPF sandbox supports TSYNC",
                ):
                    self.assertEqual(status[label], "Yes", status)
            finally:
                await client.close()
