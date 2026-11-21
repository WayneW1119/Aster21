"""Real multipart HTTP, persistent metadata and conversation-scoped downloads."""

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from relay.server import Service, make_app
from relay.store import Store


class FilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "relay.db"
        self.store = Store(self.path)
        self.owner = self.store.create(None, "owner")
        self.alice = self.store.create(self.owner["account"]["id"], "alice")
        self.bob = self.store.create(self.owner["account"]["id"], "bob")
        self.service = Service(self.store)
        self.client = TestClient(TestServer(make_app(self.service)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.db.close()

    def auth(self, account):
        return {"Authorization": "Bearer " + account["token"]}

    async def upload(
        self, account=None, data=b"hello", name="报告.txt", client_id="file-1", group=None, text=""
    ):
        form = FormData(quote_fields=False)
        form.add_field("text", text)
        form.add_field("file", data, filename=name, content_type="application/octet-stream")
        query = {"client_id": client_id, **({"group": str(group)} if group else {"to": "alice"})}
        return await self.client.post(
            "/api/files", params=query, data=form, headers=self.auth(account or self.owner)
        )

    async def test_private_upload_download_retry_inbox_and_persistence(self):
        payload = b"abc\0" * 100000  # Larger than the normal JSON body limit.
        response = await self.upload(data=payload)
        self.assertEqual(response.status, 200, await response.text())
        message = await response.json()
        file = message["attachment"]
        self.assertEqual(file["name"], "报告.txt")
        self.assertEqual(file["size"], len(payload))
        self.assertEqual(file["sha256"], hashlib.sha256(payload).hexdigest())
        again = await self.upload(data=payload)
        self.assertEqual((await again.json())["id"], message["id"])
        self.assertEqual(len(list(self.store.file_root.iterdir())), 1)
        conflict = await self.upload(data=b"different")
        self.assertEqual(conflict.status, 409)
        self.assertEqual(len(list(self.store.file_root.iterdir())), 1)
        path = "/api/files/" + file["id"]
        for account in (self.owner, self.alice):
            download = await self.client.get(path, headers=self.auth(account))
            self.assertEqual(download.status, 200)
            self.assertEqual(await download.read(), payload)
            self.assertIn("attachment;", download.headers["Content-Disposition"])
            self.assertEqual(download.headers["Content-Type"], "application/octet-stream")
            self.assertEqual(download.headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.get(path, headers=self.auth(self.bob))).status, 404)
        ranged = await self.client.get(
            path, headers={**self.auth(self.alice), "Range": "bytes=0-3"}
        )
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), payload[:4])
        inbox = self.store.inbox(self.alice["account"]["id"])
        self.assertEqual(inbox[0]["attachment"], file)
        self.assertGreater(self.store.latest(self.alice["account"]["id"]), 0)
        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.file(self.alice["account"]["id"], file["id"]), file)
        finally:
            reopened.db.close()

    async def test_group_membership_join_boundary_mute_and_revocation(self):
        owner, alice, bob = [x["account"]["id"] for x in (self.owner, self.alice, self.bob)]
        group = self.store.create_group(owner, "Group", ["alice"])["id"]
        response = await self.upload(group=group)
        self.assertEqual(response.status, 200)
        row = await response.json()
        path = "/api/files/" + row["attachment"]["id"]
        self.assertEqual(self.store.group_history(alice, group)[0]["attachment"], row["attachment"])
        self.assertEqual((await self.client.get(path, headers=self.auth(self.alice))).status, 200)
        self.store.manage_group(owner, group, "invite", members=["bob"])
        self.assertEqual((await self.client.get(path, headers=self.auth(self.bob))).status, 404)
        self.store.manage_group(owner, group, "mute", member="alice")
        denied = await self.upload(account=self.alice, group=group, client_id="muted")
        self.assertEqual(denied.status, 403)
        self.assertEqual(len(list(self.store.file_root.iterdir())), 1)
        self.store.manage_group(owner, group, "remove", member="alice")
        self.assertEqual((await self.client.get(path, headers=self.auth(self.alice))).status, 404)
        self.store.manage_group(owner, group, "invite", members=["alice"])
        self.assertEqual((await self.client.get(path, headers=self.auth(self.alice))).status, 404)
        self.store.manage_group(owner, group, "dissolve")
        self.assertEqual((await self.client.get(path, headers=self.auth(self.owner))).status, 404)

    async def test_limits_invalid_forms_and_cleanup(self):
        with patch("relay.files.MAX_FILE_SIZE", 10):
            response = await self.upload(data=b"12345678901")
            self.assertEqual(response.status, 413)
        self.assertEqual(list(self.store.file_root.iterdir()), [])
        self.assertEqual(self.store.rows("SELECT * FROM files"), [])
        form = FormData(quote_fields=False)
        form.add_field("text", "no file")
        response = await self.client.post(
            "/api/files?to=alice&client_id=missing", data=form, headers=self.auth(self.owner)
        )
        self.assertEqual(response.status, 400)
        for ident in ("unknown", "a" * 32):
            self.assertEqual(
                (
                    await self.client.get("/api/files/" + ident, headers=self.auth(self.owner))
                ).status,
                404,
            )
        response = await self.upload(
            data=b"", name="empty.html", client_id="empty", text="附带说明"
        )
        row = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(row["text"], "附带说明")
        self.assertEqual(row["attachment"]["size"], 0)
        self.store.manage(self.owner["account"]["id"], "alice", "disable")
        self.assertEqual(
            (
                await self.client.get(
                    "/api/files/" + row["attachment"]["id"], headers=self.auth(self.alice)
                )
            ).status,
            401,
        )

    async def test_cookie_upload_requires_same_origin_and_missing_auth_creates_nothing(self):
        form = FormData(quote_fields=False)
        form.add_field("file", b"data", filename="x.txt")
        response = await self.client.post("/api/files?to=alice&client_id=x", data=form)
        self.assertEqual(response.status, 401)
        self.assertEqual(list(self.store.file_root.iterdir()), [])
