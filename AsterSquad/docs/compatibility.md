# Compatibility

Squad uses the public Aster SDK and the independently installed Relay package. Current integration requirements are Harness >=0.1.21,<0.2 and Relay >=0.4,<0.5; pyproject.toml is authoritative for supported ranges. The exact revisions exercised by CI are pinned in .github/workflows/tests.yml. No sibling checkout, private deployment directory or authentication store is discovered implicitly.

- Harness owns Thread/Session state, model calls and authentication, memory/compaction, tools, skills, events and platform sandbox execution.
- Relay owns its accounts, domains, chat, browser authentication and HTTP/MCP protocol. It imports neither Harness nor Squad.
- Squad owns member hierarchy, delegation, lifecycle supervision, browser workspace and the Relay-to-Aster notification adapter.
- Web search and image generation are optional independent MCP services. Base Squad does not require their packages or credentials.

Linux/WSL uses bubblewrap and optional owner-authorized root startup. Native Windows uses MXC and optional owner-authorized UAC startup. Privileged identity keeps the saved sandbox settings; disabling it restores those settings on the original Thread. These are different OS backends with the same management policy, not interchangeable host permissions. Windows native GPU, ConPTY and Linux systemd parity are not claimed.

Upgrade matching components together. Relay 0.4 moves the former Aster-specific watch asset into Squad; do not pair an older Squad that reads that Relay asset with Relay 0.4. The installed member connection paths and notification protocol remain stable. Existing runtime records and memory are not rewritten or replaced during an upgrade. Any failed Thread recovery must use the public recovery API and validated evidence.

Core tests use scripted clients, not provider credentials. CI checks Linux Python 3.11/3.13; native Windows acceptance uses scripts/test_windows.py and a configured MXC executor. Real providers, UAC/biometric prompts and actual image jobs require operator acceptance. Passing a skipped native test does not demonstrate Windows execution.
