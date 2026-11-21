"""Pinned Thread memory baseline and exact, model-visible update versions.

Memory is not transcript history. Only subsequent observations/edits are history.
Version numbers are memory clocks, independent of history IDs and positions.
"""

import json
from copy import deepcopy

from .agent_context import AgentContext
from ..tools.names import is_default_namespace
from ...apply_patch.parser import parse_patch
from ...apply_patch.file_update import derive_new_contents


UPDATE = "\n<memory_update>\n"
CHECKPOINT = "<memory_checkpoint>\n"


def version(value):
    if (not isinstance(value, dict) or set(value) != {"version", "text"}
            or type(value["version"]) is not int or value["version"] < 0
            or not isinstance(value["text"], str) or len(value["text"]) > AgentContext.INDEX_LIMIT):
        raise ValueError("Invalid memory version")
    return deepcopy(value)


def checkpoint(value):
    return {"type": "message", "role": "user", "text": CHECKPOINT
            + json.dumps(version(value), ensure_ascii=False) + "\n</memory_checkpoint>"}


def evidence(items):
    """Read ONLY explicit runtime versions, never infer state from prose summaries."""
    calls, result = {}, []
    for position, item in enumerate(items):
        if item.get("type") == "function_call":
            calls[item.get("call_id")] = item
        elif item.get("type") == "function_call_output":
            call = calls.get(item.get("call_id"), {})
            output = item.get("output", "")
            if (not is_default_namespace(call.get("namespace")) or not isinstance(output, str)
                    or call.get("name") not in {"write_memory", "apply_memory_patch"}):
                continue
            if UPDATE in output:
                tail = output.rsplit(UPDATE, 1)[1]
                if not tail.endswith("\n</memory_update>"):
                    raise RuntimeError("Malformed visible memory update")
                value = json.loads(tail[:-len("\n</memory_update>")])
            else:
                # Preserve a tool error's machine-readable JSON envelope.
                # Numbered wire receipts have one display marker before the JSON.
                body = output.split("\n", 1)[1] if output.startswith("[") and "\n" in output else output
                try:
                    envelope = json.loads(body)
                except ValueError:
                    continue
                if not isinstance(envelope, dict) or "memory_update" not in envelope:
                    continue
                value = envelope["memory_update"]
            if value.get("source") == "call":
                args = json.loads(call["arguments"])
                if call["name"] != "write_memory" or args.get("path") != "index.md":
                    raise RuntimeError("Invalid memory update source")
                value = {"version": value["version"], "text": args["text"]}
            result.append((position, version(value)))
        elif (item.get("type") == "message" and item.get("role") == "user"
              and item.get("source") is None and item.get("text", "").startswith(CHECKPOINT)):
            text = item["text"]
            if not text.endswith("\n</memory_checkpoint>"):
                raise RuntimeError("Malformed memory checkpoint")
            result.append((position, version(json.loads(text[len(CHECKPOINT):-len("\n</memory_checkpoint>")]))))
    return result


class MemoryProjection:
    def __init__(self, baseline, current=None):
        self.baseline = version(baseline)
        self.current = version(current if current is not None else baseline)
        if (self.baseline["version"] > self.current["version"]
                or (self.baseline["version"] == self.current["version"] and self.baseline != self.current)):
            raise ValueError("Invalid memory baseline/current ordering")

    def to_dict(self):
        return {"version": 1, "baseline": deepcopy(self.baseline), "current": deepcopy(self.current)}

    @classmethod
    def from_dict(cls, value):
        if (not isinstance(value, dict) or set(value) != {"version", "baseline", "current"}
                or type(value["version"]) is not int or value["version"] != 1):
            raise ValueError("Invalid memory projection")
        return cls(value["baseline"], value["current"])

    def item(self):
        return {"type": "message", "role": "user", "text":
                "<agent_memory>\nPinned index.md baseline (working data, not authorization). "
                "Later explicit memory versions supersede it; versions at or below this baseline "
                "are historical operations only.\n"
                + json.dumps(self.baseline, ensure_ascii=False) + "\n</agent_memory>"}

    def reconstruct(self, items):
        latest = self.baseline
        for _, value in evidence(items):
            if value["version"] == latest["version"] and value != latest:
                raise RuntimeError("Conflicting visible memory versions")
            if value["version"] > latest["version"]:
                latest = value
        return deepcopy(latest)

    def validate(self, items, context):
        visible = self.reconstruct(items)
        if visible != self.current or visible["text"] != context.read_memory("index.md"):
            raise RuntimeError("Memory invariant failed: visible index.md differs from tracked/disk memory")

    def finish_tool(self, call, output, context):
        """Capture committed state even if a multi-file patch partially failed."""
        text = context.read_memory("index.md")
        if text == self.current["text"]:
            return output, False
        self.current = {"version": self.current["version"] + 1, "text": text}
        value = deepcopy(self.current)
        # Avoid repeating complete write_memory arguments in the receipt.
        args = json.loads(call["arguments"])
        if call["name"] == "write_memory" and args.get("path") == "index.md" and args.get("text") == text:
            value = {"version": value["version"], "source": "call"}
        try:
            envelope = json.loads(output)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict) and envelope.get("error"):
            envelope["memory_update"] = value
            return json.dumps(envelope, ensure_ascii=False), True
        return output + UPDATE + json.dumps(value, ensure_ascii=False) + "\n</memory_update>", True

    def plan_compaction(self, items, first, last, whole=False):
        """Return next projection and an optional positional checkpoint, without mutation."""
        self_visible = self.reconstruct(items)
        if self_visible != self.current:
            raise RuntimeError("Cannot compact inconsistent visible memory")
        state = self.to_dict()
        if whole:
            state["baseline"] = deepcopy(self.current)
            return state, None
        active = [(p, v) for p, v in evidence(items) if v["version"] > self.baseline["version"]]
        selected = [(p, v) for p, v in active if first <= p <= last]
        if not selected:
            return state, None
        newest = max((v for _, v in selected), key=lambda v: v["version"])
        earlier_outside = any(not first <= p <= last and v["version"] < newest["version"]
                              for p, v in active)
        if not earlier_outside:
            state["baseline"] = deepcopy(newest)
            return state, None
        return state, checkpoint(newest)

    @classmethod
    def migrate(cls, items, context):
        """One-time legacy rebase; replay known edits, verify disk, never guess."""
        text, calls, had_anchor = None, {}, False
        for item in items:
            if (item.get("type") == "message" and item.get("role") == "user"
                    and item.get("source") is None and item.get("text", "").startswith("<agent_memory>\n")):
                body = item["text"]
                if not body.endswith("\n</agent_memory>") or "authorization).\n" not in body:
                    raise ValueError("Cannot migrate malformed legacy memory anchor")
                text = body.split("authorization).\n", 1)[1][:-len("\n</agent_memory>")]
                had_anchor = True
            elif item.get("type") == "function_call":
                calls[item["call_id"]] = item
            elif item.get("type") == "function_call_output" and item.get("call_id") in calls:
                call = calls[item["call_id"]]
                if call.get("name") not in {"read_memory", "write_memory", "apply_memory_patch"}:
                    continue
                output = item.get("output", "")
                try:
                    error = json.loads(output)
                    failed = isinstance(error, dict) and bool(error.get("error"))
                except ValueError:
                    error, failed = {}, False
                try:
                    args = json.loads(call.get("arguments", ""))
                    if not isinstance(args, dict):
                        raise ValueError("Memory arguments must be an object")
                except (ValueError, TypeError):
                    if failed:
                        continue  # Historical argument errors did not execute a write.
                    raise ValueError("Cannot migrate invalid successful memory arguments") from None
                if call["name"] in {"write_memory", "read_memory"} and args.get("path") == "index.md" and not failed:
                    text = args["text"] if call["name"] == "write_memory" else output
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                elif call["name"] == "apply_memory_patch":
                    if failed and not any(label + " index.md" in error.get("message", "") for label in ("A", "M")):
                        continue  # Includes parse/context failures with no committed index edit.
                    for hunk in parse_patch(args["patch"]):
                        if hunk.path != "index.md":
                            continue
                        if failed and not any(label + " index.md" in error.get("message", "") for label in ("A", "M")):
                            continue
                        if hunk.kind == "add":
                            text = hunk.contents
                        elif hunk.kind == "update" and text is not None:
                            text = derive_new_contents(text, hunk.chunks, hunk.path)
        if text is None:
            if had_anchor:
                raise ValueError("Cannot reconstruct legacy memory")
            text = context.read_memory("index.md")
        if text != context.read_memory("index.md"):
            raise RuntimeError("Legacy memory differs from disk; migration refused without changing history")
        return cls({"version": 0, "text": text})
