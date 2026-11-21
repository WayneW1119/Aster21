# Development

Use Python 3.11+ and an isolated virtual environment. Install the public Aster revision and independent Relay repository as documented in README, then install `.[dev]`. Neither dependency needs a fixed sibling directory.

Run `python -m unittest discover -s tests -v`. Browser acceptance uses Playwright: install Chromium, then run `tests/browser_auth.py` and `tests/browser_console.py` (with `ASTER_BROWSER_PREFIX=/squad`). `PLAYWRIGHT_CHROMIUM_EXECUTABLE` can select an existing browser. All tests must use temporary data and scripted clients, with no paid model credentials.

Real-mode sandbox checks require Linux, bubblewrap, libseccomp and permission to create unprivileged user namespaces. Do not weaken runtime sandbox requirements to make a real-mode test pass.

Build with `python -m build`. Check packaged web assets and skills from an installed wheel outside the source tree. Preserve public SDK boundaries, caller-bound account identity, private credential storage and same-Thread recovery semantics.

Do not commit data directories, credentials, host-specific domain names, local deployment state or screenshots containing private information. Keep operator-specific deployment integrations in a separate repository or directory. Add reusable examples instead of a particular operator's live configuration.

The repository currently has no specified license. Do not assume it is MIT or add a license without the owner's decision.
