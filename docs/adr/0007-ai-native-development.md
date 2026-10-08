# ADR-0007 · AI-native development: the repo explains, checks and repairs itself

**Status**: accepted · 2026-10-07

## Context
Most changes to this project are made by coding agents (Claude Code), often unattended or from
a phone. An agent is only as good as what it can discover and verify on its own: where things
live, which invariants matter, one command that says "done", and enough runtime evidence to
diagnose a failed morning run without a human reproducing it.

## Decision
1. **`CLAUDE.md` at the root is the agent entry point** (`AGENTS.md` is a symlink for other
   tools). Short: commands, layer map, invariants, how to add a feature, where to look when the
   daily run fails. Package-level `CLAUDE.md` only where a package has rules of its own
   (`planning/`, `publish/`).
2. **One gate command**: `uv run poe check` = ruff check + ruff format --check + mypy strict +
   pytest. CI runs exactly the same task. An agent's work is done when the gate is green.
3. **Invariants are tests, not prose.** Layering (`tests/test_layering.py`), no network in tests,
   publish safety (no write without confirmation, write guard per profile), guardrail bounds,
   explanation coverage. A rule that matters gets a test that fails with a message saying how
   to fix it.
4. **Decisions are ADRs**, features are short specs in `docs/specs/` written before code
   (goal, non-goals, acceptance checks). An agent changing a decision writes a new ADR.
5. **Runtime is self-describing.** `cyp doctor --json` and `cyp run` print machine-readable
   status; every run is a `job_runs` row; logs are JSON lines. `docs/runbooks/` has one page
   per known failure (token expired, 429, schema behind, icu 5xx, write guard tripped) with the
   exact commands to diagnose and fix.
6. **Agent skills for recurring operations** in `.claude/skills/`: `morning-run` (run or verify
   today's autopilot and explain anomalies), `add-profile`, `diagnose-run`. Hooks keep the tree
   formatted after edits. Permissions allow read-only `cyp` commands and the gate; anything
   that writes to intervals.icu always asks.
7. **Small, reversible changes**: feature branches, conventional commits, PRs with CI; the
   intervals.icu calendar is only ever changed through the idempotent publisher (ADR-0005).

## Consequences
- A new session can go from "fix X" to a green gate without asking where things are.
- Slight upkeep: `CLAUDE.md`, runbooks and skills must change with the code; the gate checks
  links/paths named in `CLAUDE.md` exist so it cannot silently rot.

## Implementation notes (2026-10-07)
- `CLAUDE.md` + `AGENTS.md` (symlink); `uv run poe check` in `pyproject.toml`, used by CI.
- Drift tests: `tests/test_docs.py` (docs/01 layer table and the CLAUDE.md layer line ==
  `tests/test_layering.py`; docs/09 table == feature registry incl. defaults and requirements;
  every repo path, `cyp` command/option and `docs/NN §N` reference in all Markdown and skills
  resolves; AGENTS.md -> CLAUDE.md), `tests/test_privacy.py`, `tests/test_invariants.py` (no
  network in tests; flags read only through the sanctioned functions).
- `.claude/settings.json`: read-only `cyp` commands and the gate allowed; a PreToolUse hook
  (`.claude/hooks/ask_before_writes.sh` -> `ask_before_writes.py`) asks before anything that can
  write a calendar, a Strava ride or the schedule; see the amendment below. A PostToolUse hook
  runs `ruff format` on edited Python files.
- Skills: `.claude/skills/{morning-run,diagnose-run,add-profile}`. Runbooks: `docs/runbooks/`.
  Spec template: `docs/specs/README.md`.

## Amendment (2026-10-08)
- Decision 1: no package has rules of its own yet, so there is no package-level `CLAUDE.md`;
  one is added when a package grows them.
- Decision 5: `cyp doctor` has no `--json`; the machine-readable status is `cyp run --json`,
  `cyp profile list --json` and the saved run reports (`data/reports/autopilot/`). The runbooks
  are organised by what the morning run reports (key, write guard, publish failed, lock,
  setup/config, schedule, Strava), see `docs/runbooks/README.md`.
- The write hook judges each shell segment on its own (split on `;` `&&` `||` `|` `&` and
  newlines, comments dropped), so `--no-write` must belong to the same `cyp run`. It also asks
  before HTTP calls that POST to the write endpoints (`/v1/autopilot`, `strava-description`,
  `/plan/commit`) and before Python code that calls the write functions, and it fails closed
  (asks) on unparseable input or without `python3`. Regression cases: `tests/test_invariants.py`.
