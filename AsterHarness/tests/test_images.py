import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from PIL import Image

from agent_harness.core.client import ScriptedClient, to_api_item
from agent_harness.core.images import ImageStore, decode_data_url, normalize_image
from agent_harness.core.session import SessionConfiguration
from agent_harness.core.session.step_context import StepContext
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.context_manager.navigation import readable


def png(width=20, height=10, color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), color).save(stream, format="PNG")
    return stream.getvalue()


def attachment(data=None):
    return {"name": "test.png", "data_url": "data:image/png;base64," + base64.b64encode(data or png()).decode()}


def call(name, identifier, **arguments):
    return {"type": "function_call", "name": name, "call_id": identifier, "arguments": json.dumps(arguments)}


class ImageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store = ImageStore(self.root / "attachments", self.workspace)

    async def test_validate_mime_limits_dimensions_and_exif_orientation(self):
        data = png(3000, 1000)
        encoded, info = normalize_image(data)
        self.assertEqual((info["width"], info["height"]), (2048, 683))
        self.assertTrue(info["resized"])
        _, low = normalize_image(data, "low")
        self.assertEqual(low["width"], 512)
        stream = io.BytesIO()
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private source metadata"
        Image.new("RGB", (30, 10), "blue").save(stream, format="JPEG", exif=exif)
        normalized, rotated = normalize_image(stream.getvalue(), "original")
        self.assertEqual((rotated["width"], rotated["height"]), (10, 30))
        with Image.open(io.BytesIO(normalized)) as snapshot:
            self.assertFalse(snapshot.getexif())
        with self.assertRaisesRegex(ValueError, "MIME"):
            normalize_image(png(), declared_mime="image/jpeg")
        for url in ("https://example.com/image.png", "data:image/svg+xml;base64,AAAA", "data:image/png;base64,***"):
            with self.assertRaises(ValueError):
                decode_data_url(url)
        with self.assertRaisesRegex(ValueError, "Invalid"):
            normalize_image(b"not an image")

    async def test_atomic_snapshots_and_workspace_scope(self):
        first, second = await asyncio.gather(*[asyncio.to_thread(self.store.store, png()) for _ in range(2)])
        self.assertEqual(first, second)
        descriptor, part = first
        self.assertEqual(self.store.read_attachment(descriptor["id"]), png())
        (self.workspace / "picture.png").write_bytes(png(color="blue"))
        viewed, _ = self.store.view("picture.png")
        self.assertNotEqual(viewed["id"], descriptor["id"])
        (self.root / "outside.png").write_bytes(png())
        with self.assertRaisesRegex(ValueError, "inside"):
            self.store.view(str(self.root / "outside.png"))
        with self.assertRaises(ValueError):
            self.store.read_attachment("../outside")
        Path(descriptor["path"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.read_attachment(descriptor["id"])

    async def test_user_image_is_native_content_and_text_only_shape_is_unchanged(self):
        manager = ThreadManager(self.root / "threads")
        session = manager.create(ScriptedClient([[call("wait_for_event", "wait")]]), self.workspace)
        self.addAsyncCleanup(session.close)
        await session.post_user_message("", images=[attachment()])
        queued = session.input_queue.snapshot()[0]["item"]
        self.assertEqual(queued["source"], "terminal")
        self.assertEqual(queued["images"][0]["type"], "input_image")
        await session.run_until_idle()
        item = next(i for i in StepContext.capture(session).prompt.input if i.get("images"))
        self.assertIn("attachments", json.loads(item["text"].split("\n")[1]))
        wire = to_api_item(item)
        self.assertEqual([p["type"] for p in wire["content"]], ["input_text", "input_image"])
        self.assertFalse({"images", "_attachments", "_message_fingerprint"} & set(wire))
        self.assertEqual(to_api_item({"type": "message", "role": "user", "text": "hello"}),
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]})
        self.assertNotIn("base64", readable(queued))

    async def test_view_image_pixels_survive_resume_and_reopen_after_compact(self):
        (self.workspace / "picture.png").write_bytes(png())
        manager = ThreadManager(self.root / "threads")
        first = manager.create(ScriptedClient([[call("view_image", "view", path="picture.png")],
            [call("wait_for_event", "wait")]]), self.workspace,
            configuration=SessionConfiguration(tool_output_token_limit=256))
        self.addAsyncCleanup(first.close)
        await first.post_user_message("View image.")
        await first.run_until_idle()
        result = next(i for i in first.state.history.snapshot() if i.get("call_id") == "view" and i["type"] == "function_call_output")
        pixels = result["output"][1]["image_url"]
        descriptor = json.loads(result["output"][0]["text"])
        self.assertNotIn("base64", readable(result))
        await first.suspend()
        resumed = manager.resume(ScriptedClient([]), first.thread_id)
        self.addAsyncCleanup(resumed.close)
        same = next(i for i in resumed.state.history.snapshot() if i.get("call_id") == "view" and i["type"] == "function_call_output")
        self.assertEqual(same["output"][1]["image_url"], pixels)
        history = resumed.state.history
        resumed._compact_boundary = set(history.ids())
        resumed.compact_history(history.ids()[0], history.ids()[-1], history.generation,
                                "Image saved at " + descriptor["path"])
        (self.workspace / "picture.png").unlink()
        reopened = await resumed.router.registry.execute_async("view_image", {"path": descriptor["path"]})
        self.assertEqual(reopened.output[1]["image_url"], pixels)

    async def test_mcp_images_and_resource_blobs_promote_pixels_without_base64_prose(self):
        encoded = base64.b64encode(png()).decode()
        for raw in [
            {"content": [{"type": "text", "text": "look"}, {"type": "image", "mimeType": "image/png", "data": encoded}]},
            {"contents": [{"uri": "fixture://image", "mimeType": "image/png", "blob": encoded}]},
            {"content": [{"type": "resource", "resource": {"uri": "fixture://image", "mimeType": "image/png", "blob": encoded}}]},
        ]:
            self.assertTrue(self.store.has_mcp_images(raw))
            projected, parts, descriptors = self.store.mcp_output(raw)
            self.assertEqual(len(parts), 1)
            self.assertNotIn(encoded, json.dumps(projected))
            self.assertIn(encoded, json.dumps(raw))
            self.assertEqual(parts[0]["type"], "input_image")
            self.assertTrue(Path(descriptors[0]["path"]).is_file())

    async def test_invalid_or_excess_attachments_do_not_enqueue(self):
        session = ThreadManager(self.root / "threads").create(ScriptedClient([]), self.workspace)
        self.addAsyncCleanup(session.close)
        for images in ([attachment()] * 5, [{"name": "bad", "data_url": "file:///etc/passwd"}], "bad"):
            with self.assertRaises(ValueError):
                await session.post_user_message("inspect", images=images)
        self.assertTrue(session.input_queue.empty())

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "Install .[mcp]")
    async def test_real_mcp_image_tool_and_resource_return_native_parts(self):
        fixture = Path(__file__).parent / "fixtures/mcp_server.py"
        configuration = SessionConfiguration(mcp_servers={"fixture": {"command": sys.executable,
            "args": [str(fixture.resolve()), "--state-dir", str(self.root / "server")],
            "enabled_tools": ["image_probe"], "required": True}})
        session = ThreadManager(self.root / "threads").create(ScriptedClient([
            [{"type": "tool_search_call", "call_id": "search", "execution": "client", "arguments": {"query": "image_probe"}}],
            [{**call("image_probe", "tool_image"), "namespace": "mcp__fixture"},
             call("read_mcp_resource", "resource_image", server="fixture", uri="fixture://image")],
            [call("wait_for_event", "end")]]), self.workspace, configuration=configuration)
        self.addAsyncCleanup(session.close)
        await session.post_user_message("Inspect images.")
        await session.run_until_idle()
        for identifier in ("tool_image", "resource_image"):
            result = next(i for i in session.state.history.snapshot() if i.get("call_id") == identifier
                          and i["type"] == "function_call_output")
            self.assertIsInstance(result["output"], list)
            self.assertEqual(result["output"][1]["type"], "input_image")
