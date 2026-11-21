"""Use a configured model, stay alive across messages, and pause on /quit."""

import argparse
import asyncio
from pathlib import Path

from aster import Aster


async def main(args):
    aster = Aster(args.home)

    def show(text):
        print("\nAgent:", text, flush=True)

    agent = (
        aster.resume(args.resume, on_message=show, trace=True)
        if args.resume
        else aster.from_template(args.template, on_message=show, trace=True)
    )
    async with agent:
        print("Thread:", agent.thread_id)
        print("Config:", agent.config_path)
        print("Type /quit to pause. Messages can arrive while the Agent works.")
        while True:
            try:
                text = await asyncio.to_thread(input, "You> ")
            except EOFError:
                break
            if text.strip() == "/quit":
                break
            if text.strip():
                await agent.send(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--template", default="assistant")
    source.add_argument("--resume")
    asyncio.run(main(parser.parse_args()))
