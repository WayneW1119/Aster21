"""Model transports: offline script and streaming Responses adapter."""

import json
from ssl import SSLCertVerificationError
from http.client import IncompleteRead, RemoteDisconnected
from copy import deepcopy
from typing import Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .client_common import ModelResponse, Prompt, ResponseEvent, RetryableModelError, retry_after_seconds
from .model_protocol import ModelReply, ModelRequest
from .response_stream import collect_response, normalize_event
from .sse import iter_sse


def to_api_item(item: dict) -> dict:
    """Keep input_text/output_text as a transport detail, not our message model."""
    result = deepcopy(item)
    images = result.pop("images", [])
    result.pop("_attachments", None)
    result.pop("_message_fingerprint", None)
    if result.get("type") == "message" and "text" in result:
        text = result.pop("text")
        kind = "output_text" if result["role"] == "assistant" else "input_text"
        result["content"] = [{"type": kind, "text": text}] + images
    elif images:
        result.setdefault("content", []).extend(images)
    return result


def from_api_item(item: dict) -> dict:
    result = deepcopy(item)
    if result.get("type") == "message" and "content" in result:
        content = result.pop("content")
        if isinstance(content, str):
            result["text"] = content
        else:
            result["text"] = "\n".join(part["text"] for part in content if "text" in part)
    return result


def order_tool_outputs(items: list[dict]) -> list[dict]:
    """Finish a tool batch before exposing intervening input messages on the wire.

    Some Responses-compatible services close pending tool calls at the next user
    message. Runtime events can arrive before a tool's receipt is queued. Preserve
    their arrival order in history; only the request projection groups receipts.
    Never invent outputs for incomplete calls or otherwise repair invalid history.
    """
    ordered, deferred, pending = [], [], set()
    for item in items:
        kind = item.get("type")
        if kind in {"function_call", "tool_search_call"}:
            pending.add(item.get("call_id"))
        if (pending and kind == "message"
                and item.get("role") in ("user", "developer", "system")):
            deferred.append(item)
            continue
        ordered.append(item)
        if kind in {"function_call_output", "tool_search_output"}:
            pending.discard(item.get("call_id"))
            if not pending:
                ordered.extend(deferred)
                deferred.clear()
    if pending:
        return items
    ordered.extend(deferred)
    return ordered


def transport_for(model):
    if model.get("protocol", "responses") == "messages":
        from .messages_client import MessagesClient
        return MessagesClient
    return ResponsesClient


class ScriptedClient:
    """Deterministic stand-in, NOT a model. Real tools still execute normally."""

    def __init__(self, responses: list[list[dict]]) -> None:
        self._responses = iter(deepcopy(responses))

    def respond(self, prompt: Prompt) -> ModelResponse:
        try:
            return ModelResponse(next(self._responses))
        except StopIteration as error:
            raise RuntimeError("Offline response script exhausted") from error

    def for_step(self) -> "ScriptedClient":
        # The response iterator intentionally belongs to the whole Session.
        return self


class ResponsesClient:
    def __init__(self, endpoint: str, model: str, api_key: str,
                 *, allow_http: bool = False,
                 stream: bool = True,
                 expect_sse: bool = False,
                 allow_empty_output: bool = False,
                 cache_session_id: str | None = None,
                 trace: Callable[[str, dict], None] | None = None) -> None:
        url = urlsplit(endpoint)
        if url.scheme not in ({"https", "http"} if allow_http else {"https"}):
            raise ValueError("Use HTTPS, or --allow-http for a trusted internal HTTP endpoint")
        if not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Invalid model endpoint URL")
        if not url.path.rstrip("/").endswith("/responses"):
            raise ValueError("Use a full Responses endpoint ending in /responses")
        self.endpoint, self.model, self.api_key = endpoint, model, api_key
        self._allow_http = allow_http
        self.streaming = stream
        self.expect_sse = expect_sse
        self.allow_empty_output = allow_empty_output
        if cache_session_id is not None and (not isinstance(cache_session_id, str)
                or not 1 <= len(cache_session_id) <= 64
                or not all(33 <= ord(char) <= 126 for char in cache_session_id)):
            raise ValueError("Cache session identity must be 1–64 printable ASCII characters without spaces")
        self.cache_session_id = cache_session_id
        self.trace = trace or (lambda event, data: None)

    def for_step(self) -> "ResponsesClient":
        """Bind this request to the current transport settings, without copying services."""
        return ResponsesClient(self.endpoint, self.model, self.api_key,
                               allow_http=self._allow_http,
                               stream=self.streaming, expect_sse=self.expect_sse,
                               allow_empty_output=self.allow_empty_output,
                               cache_session_id=self.cache_session_id,
                               trace=self.trace)

    def respond(self, prompt: Prompt) -> ModelResponse:
        events = self.stream_response(prompt)
        try:
            body = collect_response(events, allow_empty_output=self.allow_empty_output)
        finally:
            events.close()
        self.trace("api_response", {"body": body})
        return ModelResponse([from_api_item(item) for item in body["output"]],
                             usage=deepcopy(body.get("usage")))

    def generate(self, request: ModelRequest) -> ModelReply:
        return ModelReply.from_response(self.respond(request.to_prompt()))

    def stream_response(self, prompt: Prompt) -> Iterator[ResponseEvent]:
        """Single HTTP request yielding normalized events; no automatic replay/retry."""
        input_items = [to_api_item(item) for item in prompt.input
                       if not (item.get("type") == "reasoning" and "messages_block" in item)]
        ordered_items = order_tool_outputs(input_items)
        if ordered_items != input_items:
            self.trace("api_transport", {"mode": "tool_outputs_before_input_messages",
                "note": "Request projection groups tool receipts; recorded event/history order is unchanged."})
        # Keep native items intact. Directory numbers live in explicit tool results,
        # never in synthetic assistant messages or prefixes on tool output.
        if prompt.memory is not None:
            ordered_items.insert(0, to_api_item(prompt.memory))
        payload = {
            "model": self.model, "instructions": prompt.base_instructions,
            "input": ordered_items,
            # Some compatible gateways emit raw model markup for 'required'.
            # Use auto for transport compatibility; Session handles scheduling.
            "tools": [{k: v for k, v in spec.items() if k != "name"}
                      if spec.get("type") == "tool_search" else spec for spec in prompt.tools], "store": False, "tool_choice": "auto",
            "include": ["reasoning.encrypted_content"],
            "stream": self.streaming,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.streaming else "application/json",
        }
        if self.cache_session_id is not None:
            payload["prompt_cache_key"] = self.cache_session_id
            headers["session-id"] = self.cache_session_id
        request = Request(self.endpoint, data=json.dumps(payload).encode(), headers=headers)
        self.trace("api_request", {"endpoint": self.endpoint, "body": payload})
        try:
            with urlopen(request, timeout=120) as response:
                headers = getattr(response, "headers", {})
                content_type = headers.get("Content-Type", "application/json").split(";", 1)[0].strip().lower()
                if content_type == "text/event-stream" or (self.expect_sse and not headers.get("Content-Type")):
                    for raw in iter_sse(response):
                        self.trace("api_stream_event", {"data": raw})
                        event = normalize_event(raw)
                        yield event
                        if event.kind in ("completed", "failed"):
                            return
                else:
                    # Compatibility with gateways that ignore stream=true. No second request.
                    body = json.load(response)
                    if self.streaming:
                        self.trace("api_transport", {"mode": "json_compatibility"})
                    if not isinstance(body, dict) or body.get("status") != "completed" or "output" not in body:
                        raise RuntimeError("Model response was not completed or had no output array")
                    yield ResponseEvent("completed", {"type": "response.completed", "response": body})
        except HTTPError as error:
            # Do not print request headers, keys, or arbitrary server error bodies.
            transient = error.code in {408, 425, 500, 502, 503, 504, 520}
            if error.code == 429:
                try:
                    detail = json.loads(error.read(65536))
                    transient = detail.get("error", {}).get("code") == "rate_limit_exceeded"
                except (ValueError, AttributeError, OSError):
                    pass
            if transient:
                raise RetryableModelError(f"Model HTTP error: {error.code}",
                    retry_after_seconds=retry_after_seconds(error.headers)) from error
            raise RuntimeError(f"Model HTTP error: {error.code}") from error
        except URLError as error:
            if isinstance(error.reason, SSLCertVerificationError):
                raise RuntimeError("Model endpoint TLS certificate verification failed") from error
            raise RetryableModelError("Could not reach model endpoint") from error
        except (OSError, IncompleteRead, RemoteDisconnected) as error:
            raise RetryableModelError("Model response transport interrupted; no output committed") from error
        except ValueError as error:
            raise RuntimeError("Model response decoding failed") from error
