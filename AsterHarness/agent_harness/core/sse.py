"""Small stdlib SSE decoder; corresponds to codex-api/src/sse transport layer."""

import json
import time
from typing import BinaryIO, Iterator

from .client_common import RetryableModelError


def iter_sse(source: BinaryIO, *, max_event_bytes: int = 4 * 1024 * 1024,
             max_duration_seconds: float = 600) -> Iterator[dict]:
    data = []
    size = 0
    first = True
    deadline = time.monotonic() + max_duration_seconds
    while True:
        # Check even comment/heartbeat lines, which never yield a model event.
        # Blocking reads retain the transport's socket timeout.
        if time.monotonic() >= deadline:
            raise TimeoutError('Model SSE response exceeded its duration limit')
        raw = source.readline(max_event_bytes + 1)
        if not raw:
            # SSE dispatch requires a blank line; an unterminated frame is not committed.
            return
        size += len(raw)
        if size > max_event_bytes:
            raise RuntimeError("Model SSE event exceeds byte limit")
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RetryableModelError("Model SSE event is not UTF-8") from None
        if first:
            line = line.removeprefix("\ufeff")
            first = False
        line = line.rstrip("\r\n")
        if not line:
            if data:
                text = "\n".join(data)
                if text == "[DONE]":
                    return  # Not a substitute for response.completed.
                try:
                    value = json.loads(text)
                except ValueError:
                    raise RuntimeError("Invalid JSON in model SSE event") from None
                if not isinstance(value, dict):
                    raise RuntimeError("Model SSE event must be an object")
                yield value
            data, size = [], 0
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
