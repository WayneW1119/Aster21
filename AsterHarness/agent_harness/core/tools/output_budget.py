"""One model-facing text budget, with lossless Thread-owned output artifacts.

Token estimates use UTF-8 bytes / 4, not a provider-specific tokenizer. Images
have separate size/count limits. Native schemas and memory evidence stay whole.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4


DEFAULT_TOOL_OUTPUT_TOKENS = 10_000


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def size(text):
    return len(text.encode("utf-8", errors="replace"))


def validate_limit(value):
    if value is not None and (type(value) is not int or not 256 <= value <= 1_000_000):
        raise ValueError("tool_output_token_limit must be 256-1000000 or None")
    return value


class ToolOutputStore:
    def __init__(self, root):
        self.root = Path(root)

    def save(self, result, call):
        identifier = "out_" + uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = json.dumps({"call": call, "result": result}, ensure_ascii=True)
        path = self.root / (identifier + ".json")
        with path.open("x", encoding="utf-8") as stream:
            path.chmod(0o600)
            stream.write(body)
        return identifier

    def read(self, output_id, offset=0, max_chars=8000, token_limit=DEFAULT_TOOL_OUTPUT_TOKENS):
        if not isinstance(output_id, str) or not re.fullmatch(r"out_[0-9a-f]{32}", output_id):
            raise ValueError("Invalid tool output ID")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative character offset")
        if type(max_chars) is not int or not 1 <= max_chars <= 100000:
            raise ValueError("max_chars must be 1-100000")
        saved = json.loads((self.root / (output_id + ".json")).read_text(encoding="utf-8"))
        output = saved["result"].get("output", saved["result"])
        text = output if isinstance(output, str) else encoded(output)
        if offset > len(text):
            raise ValueError("offset exceeds saved output length")

        def page(count):
            end = offset + count
            return encoded({"output_id": output_id, "call_id": saved["call"].get("call_id"),
                "offset": offset, "total_chars": len(text), "content": text[offset:end],
                "next_offset": end if end < len(text) else None})

        low, high = 0, min(max_chars, len(text) - offset)
        while low < high:
            middle = (low + high + 1) // 2
            if token_limit is None or size(page(middle)) <= token_limit * 4:
                low = middle
            else:
                high = middle - 1
        if low == 0 and offset < len(text):
            raise ValueError("Output budget too small for page metadata")
        return page(low)

    def limit(self, result, call, token_limit, *, memory_tool=False, protected_fields=None):
        if token_limit is None:
            return result, None
        byte_limit = token_limit * 4
        if result["type"] == "tool_search_output":
            if size(encoded(result["tools"])) <= byte_limit:
                return result, None
            identifier = self.save(result, call)
            visible = []
            total = sum(len(group["tools"]) for group in result["tools"])
            for group in result["tools"]:
                kept = {**deepcopy(group), "tools": []}
                for tool in group["tools"]:
                    candidate = visible + [{**kept, "tools": kept["tools"] + [tool]}]
                    if size(encoded(candidate)) <= byte_limit:
                        kept["tools"].append(deepcopy(tool))
                if kept["tools"]:
                    visible.append(kept)
            projected = {**result, "tools": visible}
            return projected, {"output_id": identifier, "kind": "tool_definitions", "original_tools": total,
                "visible_tools": sum(len(g["tools"]) for g in visible),
                "note": "Only complete definitions were loaded. Narrow the search; a single oversized "
                        "definition requires a larger host budget or a smaller schema. Reading the raw "
                        "artifact does not load tool definitions."}

        output = result["output"]
        text = (output if isinstance(output, str) else "\n".join(
            part["text"] for part in output if part["type"] == "input_text"))
        if size(text) <= byte_limit:
            return result, None
        identifier = self.save(result, call)
        info = {"output_id": identifier, "kind": "text", "original_chars": len(text),
                "original_utf8_bytes": size(text), "token_limit": token_limit,
                "estimate": "utf8_bytes_divided_by_4", "sha256": hashlib.sha256(text.encode(errors="replace")).hexdigest()}
        suffix, protected = "", deepcopy(protected_fields or {})
        if memory_tool:
            marker = "\n<memory_update>\n"
            if marker in text:
                text, tail = text.rsplit(marker, 1)
                suffix = marker + tail
            else:
                try:
                    envelope = json.loads(text)
                    if isinstance(envelope, dict) and "memory_update" in envelope:
                        protected["memory_update"] = envelope.pop("memory_update")
                        text = encoded(envelope)
                except ValueError:
                    pass

        def render(count):
            half = (count + 1) // 2
            preview = text[:half] + "\n[… tool output truncated …]\n" + (text[-(count // 2):] if count // 2 else "")
            return encoded({"tool_output_budget": {**info, "truncated": True,
                "note": "Incomplete preview. Use read_tool_output with output_id and character offsets "
                        "for exact pages; do not treat this preview as a complete JSON result or file."},
                "preview": preview, **protected}) + suffix

        low, high = 0, min(len(text), byte_limit)
        while low < high:
            middle = (low + high + 1) // 2
            if size(render(middle)) <= byte_limit:
                low = middle
            else:
                high = middle - 1
        limited = render(low)
        if size(limited) > byte_limit:
            # Exact version evidence is context state, not discardable business text.
            info["protected_memory_evidence_exceeds_budget"] = True
        projected = deepcopy(result)
        projected["output"] = (limited if isinstance(output, str) else
            [{"type": "input_text", "text": limited}] + [p for p in output if p["type"] == "input_image"])
        return projected, info
