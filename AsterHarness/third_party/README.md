# Third-party attribution

The Python modules in `agent_harness/apply_patch/` adapt the parser, sequence
matching and line-ending-preserving update algorithm from OpenAI Codex,
commit `014489466a1739c2c563cdfe57880c2c7c8680e3`.

- Upstream: https://github.com/openai/codex
- Source directory: `codex-rs/apply-patch/src/`
- Adaptation and fixture details: [codex/README.md](codex/README.md)
- Upstream license: [codex/LICENSE](codex/LICENSE)
- Upstream notice: [codex/NOTICE](codex/NOTICE)

Only attribution files are distributed under `third_party/codex/`; this
repository does not require or include a full Codex checkout. Design documents
may cite other upstream revisions for comparison. Those references do not
mean all upstream behavior is implemented here.
