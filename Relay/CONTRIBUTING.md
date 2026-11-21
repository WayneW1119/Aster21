# Development

Use Python 3.11+ and a virtual environment. Install `.[dev]`, run `python -m unittest discover -s tests -v`, then build with `python -m build`.

Relay must run and pass its tests without Aster or Squad installed. The optional Aster watcher is an integration asset, not a server dependency. Preserve caller-bound identity, private chat access, independent domain credentials and persistent message semantics.

Tests use temporary databases and test-only credentials. Never commit live data, tokens, host-specific deployment configuration or private chat screenshots.

The repository currently has no specified license. Do not assume it is MIT or add a license without the owner's decision.
