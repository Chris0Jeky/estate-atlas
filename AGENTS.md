# estate-atlas: agent instructions

Global laws: `~/.claude/rules/laws.md` (Claude) / `codex/AGENTS.md` in claude-config (Codex). They are binding.
Authority: `.agent-harness/tier.json` (T1 sandbox).

## What this is
A standalone, standard-library Python tool for architecture as checked data. Design: `docs/DESIGN.md`.

## Rules specific to this repository
- **It is published open source.** Never put private repository, lane, host, person or path names in code, tests,
  examples or docs. Examples are fictional (`examples/shop`).
- **Standard library only**, Python 3.11 or later. No runtime dependencies.
- **Deterministic outputs.** Renderers and generated blocks are byte-stable for the same inputs.
- **Read-only against git.** `check` reads with `git show` and never fetches, writes or checks out.

## Commands
- Tests: `python -m unittest discover -s tests -v`
- Example check: `python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop`
