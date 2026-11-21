"""A real plan-backed task, isolated from other running Agents.

Run aster auth login first, then: python examples/sdk/chatgpt_plan.py
"""

import argparse
import asyncio
from pathlib import Path

from aster import Aster, ModelConfig, SessionConfiguration
from agent_harness.agent_config import aster_home
from agent_harness.auth.chatgpt import ChatGPTAccount


async def run(args):
    auth_home = Path(args.auth_home).expanduser().absolute() if args.auth_home else aster_home()
    models = await asyncio.to_thread(ChatGPTAccount(auth_home, args.profile).models)
    model = args.model or (models[0]['slug'] if models else None)
    if not model or model not in {entry['slug'] for entry in models}:
        raise ValueError('Choose an account model with aster auth models')
    root = Path(args.directory).expanduser().absolute()
    workspace = root / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    sdk = Aster(home=root / 'state')
    completed = asyncio.Event()
    reported = False
    def message(text):
        nonlocal reported
        print(text)
        reported = True
    def trace(event, data):
        result = workspace / 'hello-aster.txt'
        if event == 'session_waiting' and reported and result.exists():
            if result.read_text().strip() == 'Aster connected.':
                completed.set()
    agent = sdk.create(workspace, model=ModelConfig(name=model, auth='chatgpt',
        auth_home=auth_home, auth_profile=args.profile),
        configuration=SessionConfiguration(max_consecutive_steps=8, filesystem_policy={}),
        instructions='用中文交流。完成用户授权的小任务并验证结果。',
        trace=True, on_message=message, on_trace=trace)
    print('Model:', model)
    print('Thread:', agent.thread_id)
    print('Config:', agent.config_path)
    print('Trace:', agent.trace_path)
    async with agent:
        await agent.send('请先用 tool_search 搜索 sample（没有匹配也正常），然后创建 '
            'hello-aster.txt，写入 Aster connected.，再用工具读取验证。'
            '完成后通过 send_user_message 告诉我结果，并设 wake_model=false。')
        await asyncio.wait_for(completed.wait(), timeout=180)
    print('Verified file:', (workspace / 'hello-aster.txt').read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-home')
    parser.add_argument('--profile', default='default')
    parser.add_argument('--model')
    parser.add_argument('--directory', default='test_runs/chatgpt-plan')
    asyncio.run(run(parser.parse_args()))
