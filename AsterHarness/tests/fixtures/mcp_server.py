"""Real stdio MCP server fixture. All business effects stay in --state-dir."""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen


parser = argparse.ArgumentParser()
parser.add_argument("--state-dir", type=Path, required=True)
parser.add_argument("--mode", default="sdk")
args = parser.parse_args()
args.state_dir.mkdir(parents=True, exist_ok=True)
(args.state_dir / "pid").write_text(str(os.getpid()))


def record(kind, **data):
    with (args.state_dir / "effects.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": kind, **data}) + "\n")


record("started", pid=os.getpid())
if args.mode == "hang":
    import time
    time.sleep(60)
elif args.mode != "sdk":
    # An independent raw protocol fixture exercises pagination, not SDK mocks.
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        if request["method"] == "initialize":
            body = {"protocolVersion": request["params"]["protocolVersion"],
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif request["method"] == "tools/list":
            cursor = request.get("params", {}).get("cursor")
            name = "first" if cursor is None else "second"
            body = {"tools": [{"name": name, "description": name,
                    "inputSchema": {"type": "object", "properties": {}}}]}
            if args.mode == "repeat_cursor" or cursor is None:
                body["nextCursor"] = "next"
            if args.mode == "invalid_schema":
                body["tools"][0]["inputSchema"] = {"type": "array"}
        else:
            body = {"content": [{"type": "text", "text": request["params"]["name"]}]}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": body}), flush=True)
else:
    from mcp.server.fastmcp import FastMCP, Image

    IMAGE_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")

    server = FastMCP("fixture", instructions="Send and read test website messages. Resources expose fixture documentation.")

    @server.tool()
    def image_probe() -> Image:
        """Return a tiny PNG image for multimodal transport tests."""
        return Image(data=IMAGE_PNG, format="png")

    @server.resource("fixture://image", name="image", mime_type="image/png")
    def image_resource() -> bytes:
        return IMAGE_PNG

    @server.resource("fixture://guide", name="guide", description="Fixture usage guide")
    def guide() -> str:
        return "Use send_message to send text. This resource does not send anything."

    @server.resource("fixture://binary", name="binary", mime_type="application/octet-stream")
    def binary() -> bytes:
        return b"\x00\xff"

    @server.resource("fixture://items/{item_id}", name="item")
    def item(item_id: str) -> str:
        return "Item " + item_id


    @server.tool()
    def send_message(text: str) -> dict[str, object]:
        """Send text to the independently configured test website."""
        record("sent", text=text)
        endpoint = os.environ.get("MCP_TEST_ENDPOINT")
        if endpoint:
            request = Request(endpoint, data=json.dumps({"text": text}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=2) as response:
                return json.load(response)
        return {"accepted": True, "text": text}

    @server.tool()
    def fail() -> str:
        """Return a deliberate business error."""
        raise ValueError("deliberate business failure")

    @server.tool()
    async def slow() -> str:
        """Side effect before a response exceeding the client's deadline."""
        record("slow")
        await asyncio.sleep(10)
        return "late"

    @server.tool()
    def crash() -> str:
        """Exit after a business effect, before returning a result."""
        record("crash")
        os._exit(7)

    @server.tool()
    def environment() -> dict[str, object]:
        """Check explicit environment forwarding."""
        return {"forwarded": os.environ.get("MCP_TEST_FORWARD"),
                "hidden": os.environ.get("MCP_TEST_HIDDEN"),
                "emission": os.environ.get("HARNESS_GENERATOR_SOCKET")}

    server.run(transport="stdio")
