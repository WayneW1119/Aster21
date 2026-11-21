"""Emit a deterministic behavior transcript for cross-implementation comparison.

Run outside the repository with PYTHONPATH selecting the implementation under
test. No model/network calls or access to the real resident Thread.
"""

import asyncio
import json
from pathlib import Path
import re
import tempfile
from unittest.mock import patch
from uuid import UUID

from agent_harness.core.client import ScriptedClient
from agent_harness.core.client_common import ModelResponse
from agent_harness.core.session import Session
from agent_harness.core.session.input_queue import HarnessEvent
from agent_harness.core.thread_manager import ThreadManager
from agent_harness.core.tools.handlers import build_file_tools
from agent_harness.core.tools.registry import Tool


def call(name, identifier, **arguments):
    return {"type": "function_call", "name": name, "call_id": identifier,
            "arguments": json.dumps(arguments, ensure_ascii=False)}


def wait(identifier):
    return call("wait_for_event", identifier)


async def transcript(root):
    result = {}
    traces = []
    trace = lambda event, data: traces.append({"event": event, **data})
    workspace = root / "workspace"
    workspace.mkdir()
    manager = ThreadManager(root / "threads")

    class MemoryClient:
        def __init__(self):
            self.step = 0

        def respond(self, prompt):
            self.step += 1
            if self.step == 1:
                output = [call("write_memory", "write", path="index.md", text="version-one")]
            elif self.step == 2:
                output = [call("apply_memory_patch", "patch", patch="*** Begin Patch\n"
                    "*** Update File: index.md\n@@\n-version-one\n+version-two\n*** End Patch")]
            elif self.step == 3:
                output = [call("read_memory", "read", path="index.md"),
                          call("query_history_directory", "directory", view_id=None,
                               offset=0, limit=50, max_chars=24000)]
            elif self.step == 4:
                directory = json.loads(next(item["output"] for item in prompt.input
                    if item.get("type") == "function_call_output"
                    and item["call_id"] == "directory"))
                output = [call("compact_history", "compact", view_id=directory["view_id"],
                    start="1", end=str(directory["item_count"]),
                    summary="Verified memory changed from version-one to version-two. Continue checking.")]
            elif self.step == 5:
                output = [call("send_user_message", "send", text="verified", wake_model=True)]
            elif self.step == 6:
                output = [call("send_user_message", "quiet", text="waiting", wake_model=False),
                          wait("wait1")]
            else:
                raise AssertionError("Unexpected extra request")
            return ModelResponse(output)

    first = manager.create(MemoryClient(), workspace, trace=trace)
    await first.post_user_message("Update memory, compact a completed stage and keep waiting.")
    await first.run_until_idle()
    assert first.state.history.generation == 1
    assert first.agent_context.read_memory("index.md").strip() == "version-two"
    assert first.state.sent_messages == ["verified", "waiting"]
    assert first.input_queue.empty()
    await first.suspend()
    resumed = manager.resume(ScriptedClient([
        [call("read_memory", "resumed_read", path="index.md")], [wait("wait2")]]),
        first.thread_id, trace=trace)
    before = resumed.state.step_number
    await resumed.run_until_idle()
    assert resumed.state.step_number == before
    await resumed.post_user_message("Continue.")
    await resumed.run_until_idle()
    await resumed.close()
    result["memory_compact_resume"] = {
        "trace": traces, "history": resumed.state.history.snapshot(),
        "archive": [resumed.state.history.item(i) for i in resumed.state.history.ids("archive")],
        "rollout": [json.loads(line) for line in resumed.rollout.path.read_text().splitlines()],
        "step": resumed.state.step_number, "pending": resumed.input_queue.snapshot()}

    traces = []
    registry = build_file_tools(workspace)
    session = None

    async def interrupt():
        await session.enqueue_event(HarnessEvent("program_event", {
            "type": "message", "role": "user", "text": "external event during tool execution"}))
        return "tool finished"

    registry.register(Tool({"type": "function", "name": "interrupt", "description": "test ingress",
        "parameters": {"type": "object", "properties": {}}}, interrupt))
    invalid = call("read_file", "invalid")
    invalid["arguments"] = "invalid JSON"
    session = Session(ScriptedClient([
        [call("interrupt", "ingress"), invalid, call("missing", "missing")],
        [{"type": "message", "role": "assistant", "text": "internal text"}]]),
        registry, workspace=workspace, trace=trace)
    await session.post_user_message("Check ingress and errors.")
    await session.run_until_idle()
    await session.close()
    result["ingress_errors_plain_text"] = {"trace": traces,
        "history": session.state.history.snapshot(), "status": session.state.status,
        "sent": session.state.sent_messages, "pending": session.input_queue.snapshot()}

    traces = []

    class UsageClient:
        def respond(self, prompt):
            identifier = "budget2" if any("context_budget_warning" in i.get("text", "")
                for i in prompt.input) else "budget1"
            return ModelResponse([wait(identifier)], {"input_tokens": 100_000})

    session = Session(UsageClient(), build_file_tools(workspace), workspace=workspace, trace=trace)
    await session.post_user_message("Budget reminder.")
    await session.run_until_idle()
    assert session.state.step_number == 2
    await session.close()
    result["context_budget"] = {"trace": traces,
        "history": session.state.history.snapshot(), "pending": session.input_queue.snapshot()}
    return result


def normalize(value, root):
    if isinstance(value, dict):
        return {key: normalize(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, root) for item in value]
    if isinstance(value, str):
        value = value.replace(str(root), "<ROOT>")
        value = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)", "<TIME>", value)
        return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>", value)
    return value


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, patch(
            "agent_harness.core.rollout.recorder.uuid4", return_value=UUID(int=1)):
        root = Path(directory).resolve()
        print(json.dumps(normalize(asyncio.run(transcript(root)), root),
                         ensure_ascii=False, sort_keys=True, indent=2))
