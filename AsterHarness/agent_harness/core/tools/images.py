"""Model-visible image viewing; pixels are native parts, never base64 prose."""

import json

from .registry import Tool
from .result import ToolResult


def register_image_tools(registry, images):
    def view_image(path, detail="auto"):
        descriptor, part = images.view(path, detail)
        return ToolResult([{"type": "input_text", "text": json.dumps(descriptor, ensure_ascii=False)}, part])

    registry.register(Tool({"type": "function", "name": "view_image", "strict": False,
        "description": "View a local PNG/JPEG/WebP/GIF as actual image input to the model. "
            "Paths resolve from the workspace; this Thread's own attachment paths are also allowed. "
            "The host snapshots the image, applies EXIF orientation and uses the first animation frame. "
            "auto/high fit within 2048 pixels per edge; low fits within 512; original preserves dimensions. "
            "Input limit 8 MiB and 25 megapixels. Returns metadata and native image content, not a textual "
            "base64 dump. Attachment snapshots persist for re-opening after compact and clean resume.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Workspace image path or a saved attachment path from this Thread."},
            "detail": {"type": "string", "enum": ["auto", "low", "high", "original"],
                       "description": "Image detail/size policy. Defaults to auto."}},
            "required": ["path"], "additionalProperties": False}}, view_image))
