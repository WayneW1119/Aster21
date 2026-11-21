"""Bounded streaming attachments; access follows the message, never a public URL."""

import asyncio
import hashlib
import os
import uuid

from aiohttp import web
from aiohttp.helpers import content_disposition_header

from .store import Problem

MAX_FILE_SIZE = 25 * 1024 * 1024


def filename(value):
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    if (
        not name
        or name in (".", "..")
        or len(name.encode("utf-8")) > 240
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise Problem("文件名无效或过长")
    return name


def install(app, service, me):
    store = service.store
    slots = asyncio.Semaphore(2)

    async def upload(request):
        actor = me(request)
        group, to = request.query.get("group"), request.query.get("to")
        client_id = request.query.get("client_id", "")
        if bool(group) == bool(to):
            raise Problem("请选择一个私聊或群聊")
        if not client_id or len(client_id) > 128:
            raise Problem("文件消息需要 1–128 字的 client_id")
        if group:
            group = int(group)
            if not store.group(actor, group)["can_send"]:
                raise Problem("当前群聊已禁言，你不能发送文件", 403)
        else:
            to = store.resolve(to)
            if to == actor or not store.enabled(to):
                raise Problem("请选择其他可用账号", 409)
        ident = uuid.uuid4().hex
        temporary = store.file_root / (".upload-" + ident)
        target = store.file_root / ident
        committed = False
        try:
            async with slots:
                reader = await request.multipart()
                seen, text, attachment = set(), "", None
                while (part := await reader.next()) is not None:
                    if part.name not in ("file", "text") or part.name in seen:
                        raise Problem("每条消息只能上传一个文件和一段文字")
                    seen.add(part.name)
                    size, checksum = 0, hashlib.sha256()
                    if part.name == "text":
                        chunks = bytearray()
                        while chunk := await part.read_chunk(64 * 1024):
                            chunks.extend(chunk)
                            if len(chunks) > 80000:
                                raise Problem("消息最长 20000 字", 413)
                        text = chunks.decode("utf-8")
                        continue
                    name = filename(part.filename)
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        while chunk := await part.read_chunk(64 * 1024):
                            size += len(chunk)
                            if size > MAX_FILE_SIZE:
                                raise Problem("单个文件最大 25 MiB", 413)
                            checksum.update(chunk)
                            await asyncio.to_thread(stream.write, chunk)
                    attachment = {
                        "id": ident,
                        "name": name,
                        "size": size,
                        "sha256": checksum.hexdigest(),
                    }
                if attachment is None:
                    raise Problem("请选择文件")
                me(request)  # Credentials/account may have been revoked during upload.
                # No await between publication and the SQLite transaction. Retries
                # compare the digest and reuse the original message, not a second file.
                os.replace(temporary, target)
                text = text.strip() or "[文件] " + attachment["name"]
                if group:
                    result = store.send_group(actor, group, text, client_id, attachment=attachment)
                else:
                    result = store.send(actor, to, text, client_id, attachment=attachment)
                committed = result["file_id"] == ident
                service.changed.set()
                return web.json_response(result)
        except (AssertionError, UnicodeError) as error:
            raise Problem("文件上传格式无效") from error
        finally:
            temporary.unlink(missing_ok=True)
            if not committed:
                target.unlink(missing_ok=True)

    async def download(request):
        row = store.file(me(request), request.match_info["id"])
        path = store.file_root / row["id"]
        if not path.is_file() or path.is_symlink():
            raise Problem("文件已不可用", 404)
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": content_disposition_header(
                    "attachment", filename=row["name"]
                ),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    app.router.add_post("/api/files", upload)
    app.router.add_get("/api/files/{id}", download)
