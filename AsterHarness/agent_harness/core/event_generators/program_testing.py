"""Bounded program self-tests using the real runner and an isolated event sink."""

import asyncio
from pathlib import Path
import tempfile
from uuid import uuid4

from .program import ProgramGenerator, validate_emission


async def test_program(script_path: Path, cwd: Path, args: list[str] | None = None,
                       timeout_ms: int = 5000, mode: str = "once", sandbox=None) -> dict:
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
        raise ValueError("timeout_ms must be an integer from 100 to 30000")
    generator = ProgramGenerator(str(script_path), str(cwd), args, mode)
    generator.sandbox = sandbox
    events = []
    # In addition to per-source rate limits, bound total retained test output.
    max_events = 20

    async def collect(kind, data):
        validate_emission(generator.attempts, kind, data)
        if len(events) >= max_events:
            return {"status": "rejected", "scope": "test", "reason": "test_event_limit"}
        ident = "test_" + uuid4().hex
        events.append({"event_id": ident, "kind": kind, "data": data})
        return {"status": "accepted", "scope": "test", "event_id": ident}

    with tempfile.TemporaryDirectory(prefix="harness-program-test-") as directory:
        generator.bind("gen_" + uuid4().hex, Path(directory))
        status, error = "completed", None
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                await generator.run(collect)
        except TimeoutError:
            status, error = "timed_out", "Test time limit reached; owned process group stopped"
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        # run() also performs process-group cleanup on timeout, failure or cancellation.
        output = (generator.log_path.read_bytes().decode("utf-8", errors="replace")
                  if generator.log_path.exists() else "")
        return {"scope": "test", "status": status, "mode": mode,
                "exit_code": generator.process.returncode if generator.process else None,
                "error": error, "output": output, "events": events,
                "state_persisted": False,
                "note": "No registration or real program events. Temporary state discarded. "
                        "Not a sandbox: script file/network side effects are real. "
                        "A service timeout is not proof of successful operation."}
