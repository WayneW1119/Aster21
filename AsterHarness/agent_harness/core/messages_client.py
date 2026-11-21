"""Anthropic Messages adapter for the Aster model boundary."""
import hashlib
import json
import re
from copy import copy
from http.client import IncompleteRead
from ssl import SSLCertVerificationError
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .client_common import RetryableModelError, retry_after_seconds
from .sse import iter_sse
from .model_protocol import ModelItem, ModelReply, ModelRequest


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _image(part):
    url = part["image_url"]
    if url.startswith("data:"):
        media, separator, data = url[5:].partition(";base64,")
        if not separator:
            raise ValueError("Expected a base64 image")
        source = {"type": "base64", "media_type": media, "data": data}
    else:
        source = {"type": "url", "url": url}
    return {"type": "image", "source": source}


def _name(namespace, name):
    if namespace in (None, "", "functions") and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        return name
    prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{namespace or 'tool'}__{name}")
    suffix = hashlib.sha256(json.dumps([namespace, name]).encode()).hexdigest()[:16]
    return f"{prefix[:47]}_{suffix}"


def _tools(specs):
    definitions, names = {}, {}
    def add(spec, namespace=None):
        kind = spec["type"]
        if kind == "namespace":
            for child in spec["tools"]:
                add(child, spec["name"])
            return
        if kind not in ("function", "tool_search"):
            raise ValueError("Unsupported Messages tool type: " + kind)
        name = spec.get("name") or "tool_search"
        wire = _name(namespace, name)
        identity = (namespace, name, kind)
        if wire in names and names[wire] != identity:
            raise ValueError("Conflicting Messages tool names")
        names[wire] = identity
        definitions[wire] = {"name": wire, "description": spec.get("description", ""),
                             "input_schema": spec["parameters"]}
    for spec in specs:
        add(spec)
    return list(definitions.values()), names


def project(request, model, origin):
    system = [{"type": "text", "text": request.instructions}] if request.instructions else []
    messages = []
    def append(role, blocks):
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})
    specs = list(request.tools)
    for item in request.history:
        if item.kind in ("discovery_result", "tool_definitions"):
            specs.extend(item.fields.get("tools", []))
    tools, names = _tools(specs)
    pending, deferred = set(), []
    for item in ([request.memory] if request.memory else []) + request.history:
        fields = item.fields
        if item.kind == "message":
            role = fields["role"]
            blocks = [{"type": "text", "text": fields["text"]}] if fields.get("text") else []
            blocks.extend(_image(part) for part in fields.get("images", []))
            if role in ("developer", "system"):
                system.extend(blocks)
            elif pending and role == "user":
                deferred.extend(blocks)
            else:
                append(role, blocks)
        elif item.kind in ("tool_call", "tool_discovery"):
            search = item.kind == "tool_discovery"
            name = "tool_search" if search else fields["name"]
            args = fields["arguments"]
            append("assistant", [{"type": "tool_use", "id": fields["call_id"],
                                  "name": _name(fields.get("namespace"), name),
                                  "input": json.loads(args) if isinstance(args, str) else args}])
            pending.add(fields["call_id"])
        elif item.kind in ("tool_result", "discovery_result"):
            value = fields.get("output", fields.get("tools", []))
            if isinstance(value, list) and item.kind == "tool_result":
                content = [_image(part) if part.get("type") == "input_image" else
                           {"type": "text", "text": part["text"]} for part in value]
            else:
                content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            append("user", [{"type": "tool_result", "tool_use_id": fields["call_id"], "content": content}])
            pending.discard(fields["call_id"])
            if not pending:
                append("user", deferred)
                deferred = []
        elif item.kind == "reasoning":
            if fields.get("messages_origin") == origin:
                append("assistant", [fields["messages_block"]])
        elif item.kind != "tool_definitions":
            raise ValueError("Unsupported history item for Messages: " + str(item.record_type or item.kind))
    if pending:
        raise ValueError("Messages history contains an unfinished tool call")
    payload = {"model": model, "max_tokens": 8192, "messages": messages}
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools
    return payload, names


def collect_message(events):
    message = None
    active = None
    fragments = []
    finished = False
    for event in events:
        kind = event["type"]
        if kind == "ping":
            continue
        if kind == "error":
            if event.get("error", {}).get("type") in ("overloaded_error", "api_error", "rate_limit_error"):
                raise RetryableModelError("Temporary Messages stream failure")
            raise RuntimeError("Messages stream failed")
        if kind == "message_start" and message is None:
            message = {**event["message"], "usage": dict(event["message"]["usage"])}
            message["content"] = []
        elif message is None:
            raise RuntimeError("Missing Messages stream start")
        elif finished and kind != "message_stop":
            raise RuntimeError("Unexpected event after Messages stop reason")
        elif kind == "content_block_start":
            if active is not None or event["index"] != len(message["content"]):
                raise RuntimeError("Invalid Messages block sequence")
            active = dict(event["content_block"])
            fragments = []
        elif kind in ("content_block_delta", "content_block_stop"):
            if active is None or event["index"] != len(message["content"]):
                raise RuntimeError("Unexpected Messages block index")
            if kind == "content_block_stop":
                if fragments:
                    active["input"] = json.loads("".join(fragments))
                message["content"].append(active)
                active = None
            else:
                delta = event["delta"]
                field = {"text_delta": "text", "thinking_delta": "thinking",
                         "signature_delta": "signature", "input_json_delta": "partial_json"}.get(delta["type"])
                expected = {"text": "text", "thinking": "thinking", "signature": "thinking",
                            "partial_json": "tool_use"}.get(field)
                if expected != active["type"]:
                    raise RuntimeError("Messages delta does not match its block")
                if field == "partial_json":
                    fragments.append(delta[field])
                elif field:
                    active[field] = active.get(field, "") + delta[field]
                else:
                    raise RuntimeError("Unsupported Messages block delta")
        elif kind == "message_delta":
            if active is not None:
                raise RuntimeError("Messages stop reason before block end")
            message["stop_reason"] = event["delta"].get("stop_reason")
            finished = bool(message["stop_reason"])
            message["usage"].update(event.get("usage", {}))
        elif kind == "message_stop" and active is None and finished:
            return message
        else:
            raise RuntimeError("Unexpected Messages stream event")
    raise RetryableModelError("Messages stream ended before message_stop")


class MessagesClient:
    def __init__(self, endpoint, model, api_key, *, allow_http=False, trace=None):
        url = urlsplit(endpoint)
        if (url.scheme not in ({"http", "https"} if allow_http else {"https"})
                or not url.hostname or url.username or url.password or url.query or url.fragment
                or not url.path.rstrip("/").endswith("/messages")):
            raise ValueError("Use a full HTTPS Messages endpoint ending in /messages")
        self.endpoint, self.model, self.api_key = endpoint, model, api_key
        self.trace = trace or (lambda *_: None)

    def for_step(self):
        return copy(self)

    def respond(self, prompt):
        return self.generate(ModelRequest.from_prompt(prompt)).to_response()

    def generate(self, request):
        origin = [self.endpoint, self.model]
        payload, names = project(request, self.model, origin)
        payload["stream"] = True
        http = Request(self.endpoint, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": self.api_key})
        self.trace("api_request", {"endpoint": self.endpoint, "body": payload})
        try:
            with build_opener(_NoRedirect).open(http, timeout=120) as stream:
                body = (collect_message(iter_sse(stream)) if stream.headers.get_content_type() == "text/event-stream"
                        else json.load(stream))
        except HTTPError as error:
            if error.code in (408, 429, 500, 502, 503, 504, 529):
                raise RetryableModelError(f"Messages HTTP error: {error.code}",
                    retry_after_seconds=retry_after_seconds(error.headers)) from error
            raise RuntimeError(f"Messages HTTP error: {error.code}") from error
        except URLError as error:
            if isinstance(error.reason, SSLCertVerificationError):
                raise RuntimeError("Messages TLS certificate verification failed") from error
            raise RetryableModelError("Could not reach Messages endpoint") from error
        except (OSError, IncompleteRead) as error:
            raise RetryableModelError("Messages transport interrupted") from error
        self.trace("api_response", {"body": body})
        if body.get("type") != "message" or body.get("stop_reason") not in ("tool_use", "end_turn", "stop_sequence"):
            raise RuntimeError("Messages response incomplete: " + str(body.get("stop_reason")))
        items = []
        for block in body["content"]:
            kind = block["type"]
            if kind == "text":
                items.append(ModelItem("message", {"role": "assistant", "text": block["text"]}))
            elif kind in ("thinking", "redacted_thinking"):
                items.append(ModelItem("reasoning", {"messages_origin": origin, "messages_block": block}))
            elif kind == "tool_use":
                namespace, name, tool_type = names[block["name"]]
                if not isinstance(block["input"], dict):
                    raise RuntimeError("Invalid Messages tool input")
                if tool_type == "tool_search":
                    items.append(ModelItem("tool_discovery", {"call_id": block["id"], "arguments": block["input"], "execution": "client"}))
                else:
                    items.append(ModelItem("tool_call", {"name": name, "call_id": block["id"],
                        "arguments": json.dumps(block["input"], ensure_ascii=False),
                        **({"namespace": namespace} if namespace else {})}))
            else:
                raise RuntimeError("Unsupported Messages response block: " + kind)
        raw = body["usage"]
        cached = raw.get("cache_read_input_tokens", 0)
        inputs = raw["input_tokens"] + cached + raw.get("cache_creation_input_tokens", 0)
        return ModelReply(items, {"input_tokens": inputs, "output_tokens": raw["output_tokens"],
            "total_tokens": inputs + raw["output_tokens"], "input_tokens_details": {"cached_tokens": cached}})
