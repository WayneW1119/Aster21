"""Explicit host-only account management; never an Agent tool."""

import argparse
import json
from .agent_config import aster_home
from .auth.chatgpt import ChatGPTAccount, ChatGPTAuthError, RESOURCE


def main(argv=None):
    parser = argparse.ArgumentParser(prog='aster auth', description='Connect Aster to your ChatGPT plan')
    parser.add_argument('action', choices=['login', 'status', 'models', 'probe', 'logout'])
    parser.add_argument('--home', default=None)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=54321)
    parser.add_argument('--timeout', type=int, default=600, help='Seconds to wait for browser authorization')
    parser.add_argument('--model')
    args = parser.parse_args(argv)
    account = ChatGPTAccount(args.home or aster_home(), args.profile)
    try:
        if args.action == 'login':
            result = account.login(port=args.port, timeout=args.timeout, open_browser=not args.no_browser)
        elif args.action == 'status':
            result = account.status()
        elif args.action == 'models':
            result = account.models()
        elif args.action == 'logout':
            result = account.logout()
        else:
            from .core.chatgpt_client import ChatGPTResponsesClient
            from .core.client_common import Prompt
            models = account.models()
            model = args.model or (models[0]['slug'] if models else None)
            if not model or model not in {m['slug'] for m in models}:
                raise ChatGPTAuthError('Choose an available model with aster auth models')
            client = ChatGPTResponsesClient(args.home or aster_home(), model, profile=args.profile)
            response = client.respond(Prompt('Reply briefly.', [
                {'type': 'message', 'role': 'user', 'text': 'Say exactly: Aster connected.'}], []))
            result = {'model': model, 'output': response.output, 'usage': response.usage,
                      'billing': 'ChatGPT plan', 'manage_usage': 'https://chatgpt.com/settings/usage'}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ChatGPTAuthError, OSError, RuntimeError) as error:
        parser.exit(1, str(error) + '\n')
