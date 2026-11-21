"""Validated, immutable image snapshots shared by user attachments and tools."""

import base64
from copy import deepcopy
import hashlib
import io
import os
from pathlib import Path
import re
import warnings
from uuid import uuid4


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 4
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


def decode_data_url(value):
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 512:
        raise ValueError("Image data URL exceeds 8 MiB; resize the image first")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\r\n]+)", value)
    if match is None:
        raise ValueError("Use a base64 PNG, JPEG, WebP or GIF image data URL")
    try:
        data = base64.b64decode(match[2].replace("\n", "").replace("\r", ""), validate=True)
    except ValueError:
        raise ValueError("Invalid base64 image") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds 8 MiB")
    return data, match[1]


def normalize_image(data, detail="auto", declared_mime=None):
    from PIL import Image, ImageOps

    if detail not in {"auto", "low", "high", "original"}:
        raise ValueError("detail must be auto, low, high or original")
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image must contain 1 byte to 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in MIMES:
                    raise ValueError("Supported formats: PNG, JPEG, WebP, GIF")
                if declared_mime is not None and declared_mime != MIMES[original.format]:
                    raise ValueError("Image MIME type does not match its contents")
                if original.width * original.height > 25_000_000:
                    raise ValueError("Image exceeds 25 megapixels; resize it first")
                original.seek(0)  # Animated inputs intentionally use their first frame.
                image = ImageOps.exif_transpose(original)
                source_size = image.size
                image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
                image.info.clear()  # Do not carry EXIF or other source metadata into snapshots.
                if detail != "original":
                    edge = 512 if detail == "low" else 2048
                    image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                while True:
                    stream = io.BytesIO()
                    image.save(stream, format="PNG")
                    encoded = stream.getvalue()
                    if len(encoded) <= MAX_IMAGE_BYTES:
                        break
                    if detail == "original":
                        raise ValueError("Normalized original exceeds 8 MiB; use detail=auto")
                    image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)), Image.Resampling.LANCZOS)
                return encoded, {"mime_type": "image/png", "width": image.width, "height": image.height,
                    "original_width": source_size[0], "original_height": source_size[1],
                    "resized": image.size != source_size, "detail": detail}
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Invalid or oversized image: " + str(error)) from None


class ImageStore:
    def __init__(self, root, workspace):
        self.root, self.workspace = Path(root).resolve(), Path(workspace).resolve()
        self.sandbox = None

    def store(self, data, *, name="image", detail="auto", declared_mime=None):
        normalized, info = normalize_image(data, detail, declared_mime)
        identifier = hashlib.sha256(normalized).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / (identifier + ".png")
        temporary = self.root / (".image-" + uuid4().hex)
        try:
            with temporary.open("xb") as stream:
                temporary.chmod(0o600)
                stream.write(normalized)
            try:
                os.link(temporary, path)  # Readers only ever see a complete snapshot.
            except FileExistsError:
                if path.read_bytes() != normalized:
                    raise RuntimeError("Image attachment checksum mismatch")
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = {"id": identifier, "path": str(path), "name": name[:200], **info}
        part = {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(normalized).decode(),
                "detail": detail}
        return descriptor, part

    def read_attachment(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise ValueError("Invalid image attachment ID")
        data = (self.root / (identifier + ".png")).read_bytes()
        if hashlib.sha256(data).hexdigest() != identifier:
            raise ValueError("Image attachment checksum mismatch")
        return data

    def view(self, path, detail="auto"):
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a nonempty image path")
        target = (self.workspace / path).resolve(strict=True)
        if self.sandbox is not None and self.sandbox.enabled and not target.is_relative_to(self.root):
            data = base64.b64decode(self.sandbox.file_operation('bytes', path=path, limit=MAX_IMAGE_BYTES))
            return self.store(data, name=target.name, detail=detail)
        if not (target.is_relative_to(self.workspace) or target.is_relative_to(self.root)):
            raise ValueError("Image must be inside the workspace or this Thread's attachments")
        with target.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
        return self.store(data, name=target.name, detail=detail)

    def user_message(self, text, images=None):
        images = [] if images is None else images
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise ValueError("Attach at most 4 images")
        if not isinstance(text, str) or not text.strip() and not images:
            raise ValueError("User message must contain text or images")
        item = {"type": "message", "role": "user", "source": "terminal", "text": text}
        if not images:
            return item
        parts, attachments, total = [], [], 0
        for entry in images:
            if (not isinstance(entry, dict) or set(entry) - {"name", "data_url", "detail"}
                    or not isinstance(entry.get("name", "image"), str)):
                raise ValueError("Each image requires data_url and optional name/detail")
            data, mime = decode_data_url(entry.get("data_url"))
            total += len(data)
            if total > MAX_MESSAGE_IMAGE_BYTES:
                raise ValueError("Images exceed 20 MiB combined")
            descriptor, part = self.store(data, name=entry.get("name", "image"),
                detail=entry.get("detail", "auto"), declared_mime=mime)
            attachments.append(descriptor)
            parts.append(part)
        item.update(images=parts, _attachments=attachments)
        return item

    @staticmethod
    def has_mcp_images(result):
        return (any(block.get("type") == "image" or block.get("type") == "resource"
                    and str(block.get("resource", {}).get("mimeType", "")).startswith("image/")
                    and "blob" in block.get("resource", {}) for block in result.get("content", []))
                or any(str(block.get("mimeType", "")).startswith("image/") and "blob" in block
                       for block in result.get("contents", [])))

    def mcp_output(self, result):
        """Promote standard MCP image blocks to native image parts, preserving raw elsewhere."""
        projected = deepcopy(result)
        images, descriptors = [], []
        candidates = []
        for block in projected.get("content", []):
            if block.get("type") == "image":
                candidates.append((block, "data"))
            elif block.get("type") == "resource":
                resource = block.get("resource", {})
                if str(resource.get("mimeType", "")).startswith("image/") and "blob" in resource:
                    candidates.append((resource, "blob"))
        for block in projected.get("contents", []):
            if str(block.get("mimeType", "")).startswith("image/") and "blob" in block:
                candidates.append((block, "blob"))
        for block, binary_field in candidates:
            raw = block.pop(binary_field, "")
            if len(images) >= MAX_IMAGES:
                block["image_note"] = "Omitted: maximum 4 model-visible images per tool result"
                continue
            try:
                data, mime = decode_data_url("data:" + block.get("mimeType", "") + ";base64," + raw)
                descriptor, part = self.store(data, name="MCP image", declared_mime=mime)
                descriptors.append(descriptor)
                images.append(part)
                block.update(attachment=descriptor)
            except ValueError as error:
                block["image_error"] = str(error)
        return projected, images, descriptors
