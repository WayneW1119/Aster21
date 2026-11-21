# Codex apply-patch attribution

Source: the user-provided OpenAI Codex checkout, commit
`014489466a1739c2c563cdfe57880c2c7c8680e3`.

The Python modules in `agent_harness/apply_patch/` adapt the patch parser,
sequence matching and line-ending-preserving update algorithm from
`codex-rs/apply-patch/src/`. They are a simplified adaptation, not a full port.
The original Apache-2.0 license and upstream notice are retained in
[LICENSE](LICENSE) and [NOTICE](NOTICE). No Ratatui code is used by this adaptation;
its attribution is retained as part of the upstream notice.

`tests/fixtures/apply_patch/codex_scenarios.json` is a mechanically encoded copy
of all 25 directories under `codex-rs/apply-patch/tests/fixtures/scenarios/`.
Original names, patch text, file bytes (UTF-8 encoded in JSON), and directory
entries are preserved. Each case asserts final filesystem state, as the original
scenario suite does; failure cases can intentionally leave earlier changes.

Our tool uses JSON function arguments instead of Codex's freeform tool schema.
Additional local boundaries: workspace-only paths, no symlinks, 1 MiB patch,
100 operations, 8 MiB updated files. Multi-environment patch IDs, streaming patch
parsing, approval/sandbox orchestration, diff UI and undo are not implemented.
