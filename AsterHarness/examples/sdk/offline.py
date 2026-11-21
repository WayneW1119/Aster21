"""No model credentials: exercise actual tools, events, message output and resume."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from aster import Aster, ScriptedClient


def call(name, **arguments):
    return {
        "type": "function_call",
        "call_id": uuid4().hex,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }


async def demo(directory: Path):
    workspace = directory.resolve() / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "hello.txt").write_text("Hello from a real file.\n", encoding="utf-8")
    aster = Aster(directory / "state")
    client = ScriptedClient(
        [
            [call("read_file", path="hello.txt")],
            [
                call(
                    "send_user_message",
                    text="离线脚本已执行真实文件读取。这是预设回复，不是模型理解。",
                    wake_model=False,
                )
            ],
        ]
    )
    agent = aster.create(workspace, client=client, trace=True)
    async with agent.messages() as messages, agent:
        await agent.emit("demo_started", {"file": "hello.txt"})
        message = await asyncio.wait_for(anext(messages), 10)
        print(message.text)
        print("Agent is still alive:", not agent.is_closed)
        # This application chooses to stop here; a message is not an end marker.
    print("Thread:", agent.thread_id)
    print("Config:", agent.config_path)
    print("Full trace:", agent.trace_path)
    resumed = aster.resume(agent.thread_id, client=ScriptedClient([]))
    async with resumed:
        print("Resumed the same Thread:", resumed.thread_id == agent.thread_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("test_runs/sdk-demo"))
    asyncio.run(demo(parser.parse_args().directory))
