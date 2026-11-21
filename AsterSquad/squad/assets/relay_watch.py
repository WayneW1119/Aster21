"""Squad Relay notification adapter for the Aster event protocol. Configure via --connection FILE.

Read-only: never acknowledges messages. Notifications are hints; fetch originals
through Relay MCP. Restart and notifications may repeat. No secrets in events.
"""
import argparse
import json
import time
import urllib.error
import urllib.request

from aster.events import emit_event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", required=True)
    parser.add_argument("--listen-name", help="Account alias to include in notifications")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    with open(args.connection, encoding="utf-8") as stream:
        config = json.load(stream)

    def get(path):
        request = urllib.request.Request(config["url"] + path, headers={"Authorization": "Bearer " + config["token"]})
        with urllib.request.urlopen(request, timeout=35) as response:
            return json.load(response)

    cursor = 0
    last_notice = 0
    delay = 2
    while True:
        try:
            result = get(f"/api/wait?after={cursor}&timeout={0 if args.self_test else 25}")
            pending = result["pending"]
            # At most one batch notification every 5s, plus a 60s reminder.
            if pending and (result["latest"] > cursor or time.monotonic() - last_notice >= 60):
                payload = {"username": config["username"], "pending": pending}
                if args.listen_name:
                    payload["listen_name"] = args.listen_name
                receipt = emit_event("relay_messages_available", payload)
                if receipt["status"] == "accepted":
                    last_notice = time.monotonic()
                    cursor = result["latest"]
            else:
                cursor = result["latest"]
            delay = 2
            if args.self_test:
                print(json.dumps({"connected": True, "pending": pending}))
                return
            time.sleep(5)
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise RuntimeError("Relay account disabled or credential revoked; reconnect account") from None
            if args.self_test:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)
        except (OSError, ValueError):
            if args.self_test:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)


if __name__ == "__main__":
    main()
