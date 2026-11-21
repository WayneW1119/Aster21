"""Blocking event SDK for registered sources and isolated program tests (Unix)."""

import json
import os
import socket
from pathlib import Path


def _runtime_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            "No program-generator runtime connected. Importing the SDK does not enable "
            "event emission. Use test_program_generator for isolated testing, or "
            "register_program_generator for real events; ordinary exec_command has no "
            "event channel or generator state directory.")
    return value


def state_directory() -> Path:
    """Private runtime state; persistent for registered sources, temporary in tests."""
    return Path(_runtime_value("HARNESS_GENERATOR_STATE_DIR"))


def emit_event(kind: str, data: dict) -> dict:
    """Request emission. accepted means queued, not processed or durably committed.

    A rejected event is not automatically replayed. Retain business cursors/pending
    data until acceptance if necessary; duplicates across restart remain possible.
    Test runtimes return scope=test receipts, never enqueue real events. An ordinary
    exec_command has no runtime: this function raises a clear RuntimeError.
    """
    descriptor = os.environ.get("HARNESS_GENERATOR_FD")
    socket_path = _runtime_value("HARNESS_GENERATOR_SOCKET") if descriptor is None else None
    message = {"kind": kind, "data": data}
    if os.name == 'nt' and socket_path and socket_path.startswith('tcp:'):
        message['token'] = _runtime_value('HARNESS_GENERATOR_TOKEN')
    request = json.dumps(message, ensure_ascii=False,
                         allow_nan=False).encode() + b"\n"
    if len(request) > 16384 + (256 if os.name == "nt" else 0):
        return {"status": "rejected", "reason": "event_too_large"}
    tcp = os.name == 'nt' and socket_path and socket_path.startswith('tcp:')
    connection = socket.socket(socket.AF_INET if tcp else socket.AF_UNIX) if descriptor is None else socket.socket(fileno=os.dup(int(descriptor)))
    with connection:
        connection.settimeout(10)
        if socket_path is not None:
            connection.connect(('127.0.0.1', int(socket_path.rsplit(':', 1)[1])) if tcp else socket_path)
        connection.sendall(request)
        with connection.makefile("rb") as stream:
            response = stream.readline(4096)
    if not response or len(response) >= 4096:
        raise RuntimeError("Generator manager returned no valid acknowledgement")
    return json.loads(response)
