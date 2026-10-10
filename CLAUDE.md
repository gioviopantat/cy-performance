# cy-performance: agent guide

Cycling analysis + adaptive training plans written to intervals.icu, for several athletes
(profiles) on one always-on Mac. Python 3.12+, `uv`, typer CLI `cyp`. Docs are English; anything
the athlete reads (reports, explanations, workout notes) is zh-TW.

## Done means
`uv run poe check` is green (ruff, format, mypy strict, pytest, web: API types + tsc + build;
CI runs exactly this), **and**
the docs below match the code in the same commit. Format/autofix: `uv run poe fmt`.

## Always update docs with code
| You changed | Update |
|-------------|--------|
| a package, module or import direction | `docs/01-architecture.md` §5 + layer table (§5.7.1) and `tests/test_layering.py` `LAYERS` (a test compares them) |
| a running unit, data store, external service or caller | `docs/architecture.c4` (LikeC4 container diagram; `npx likec4 validate docs`) |
| an optional capability | a flag in `src/cyp/core/features.py` + `docs/09-features.md` (a test checks) |
| a decision or trade-off | a new ADR in `docs/adr/` (never rewrite an accepted one; supersede it) |
| a command or workflow | `README.md` and this file |
| a failure mode the morning run can hit | a page in `docs/runbooks/` |
| a feature bigger than a bug fix | write `docs/specs/<name>.md` first (template in `docs/specs/README.md`) |

## Layers (low -> high; import only from your layer or lower; `tests/test_layering.py`)
0 `core` · 1 `settings` `logging` `schemas` `profiles` · 2 `store` · 3 `dataset` `ingest` ·
4 `analysis` · 5 `planning` `reports` · 6 `publish` · 7 `jobs` `services` `llm` ·
8 `cli` `api` `devtools`. CLI and API call `services/`, never models directly.

## Invariants (each one is a test)
- **Personal data never in git.** `profiles/` and `config/athlete.yaml` are git-ignored; tests use
  `tests/fixtures/athlete.reference.yaml` (invented). `tests/test_privacy.py`.
- **Tests never touch the network or the real data dir**: sockets other than loopback raise,
  notifications are stubbed, cwd is a tmp dir (`tests/conftest.py`, `tests/test_invariants.py`).
- **The calendar is written only by `services/publish.publish_plan()`** with `write=True`
  (plus the throw-away `cyp publish spike` event). It requires `publish.calendar` on, a valid
  athlete.yaml, a known upsert mode and the **write guard** (API key owner == profile
  `icu_athlete_id`; fails closed without it). A failed write raises `PublishFailedError`. Only
  events with our `external_id` are touched (ADR-0005).
- **Never write a plan that needs review**, from the autopilot or `cyp plan --apply`.
- **The only Strava write is `services/strava_write.push()`**: flag `strava.write_description`,
  `activity:write` scope, token owner == ride owner, explicit confirmation; keeps the athlete's
  own text and replaces only our RIDE.LOG block.
- **The web UI writes only through those same functions** (ADR-0009): `confirm=true`, the
  feature flag, the profile lock; `cyp serve` refuses a non-loopback bind without
  `CYP_API_TOKEN` and checks the `Host` header on loopback.
- **The autopilot writes only for profiles in `planner.mode: apply`**. Promotion is a human
  step: `cyp profile promote <name>`.
- **A profile run is hermetic**: settings come only from `profiles/<name>/.env` or defaults
  (`settings.PROFILE_INHERITABLE` lists the exceptions), and every intervals.icu sync first
  checks that the key belongs to the profile's athlete.
- **Flags**: ask only via `ctx.features().enabled("<id>")`; unknown ids raise
  (`tests/test_invariants.py`).
- **Every run leaves evidence**: `cyp run` always returns a report and a `job_runs` row
  (`config` / `lock` / `setup` failures included).
- **Deterministic planner**; an LLM may narrate, never compute numbers (ADR-0004).

## Map
| Where | What |
|-------|------|
| `src/cyp/services/autopilot.py` | one profile's morning: sync → analyze → reports → plan → publish |
| `src/cyp/services/publish.py` | calendar writes + write guard |
| `src/cyp/services/profiles.py` | onboarding, adopt, profile status (CLI stays thin) |
| `src/cyp/profiles.py`, `src/cyp/settings.py` | profile store, settings resolution, flags bridge |
| `src/cyp/core/features.py` | flag registry |
| `src/cyp/jobs/runner.py`, `src/cyp/jobs/launchd.py` | `run_profile` (lock, migrate, notify), `run --all`; launch agent |
| `docs/onboarding-questions.md` | the questions a new athlete answers, and what they drive |
| `src/cyp/api/`, `web/` | HTTP API (one server, every profile via `X-CYP-Profile`) and the React dashboard; after changing a schema run `uv run cyp dev openapi` and `npm --prefix web run gen:api` |
| `src/cyp/planning/` | season, week planner (`menus.py`: goal emphasis, intensity, variety), athlete rules, guardrails, workout library (45 templates) |
| `src/cyp/cli/` | typer commands; `common.py` has the shared options/context |
| `docs/` | architecture (01), integrations (02), data model (03), analysis (04), training (05), roadmap (06), explainability (07), API (08), flags (09) |

## Operating it
```bash
uv run cyp profile list                  # who, mode, last autopilot run
uv run cyp --profile <name> features     # what will run and why
uv run cyp --profile <name> run --no-write --json   # a dry run of the morning
uv run cyp schedule status               # is the 05:30 agent loaded; last exit code
uv run cyp --profile <name> doctor       # config, DB, tokens, last job runs
```
Morning run failed? `docs/runbooks/README.md`. Never run `cyp ride-log push --confirm-write`,
`cyp run` without `--no-write`,
`cyp plan --apply --confirm-write`, `cyp profile promote` or `cyp publish spike --confirm-write`
without the user asking: they write to a real person's calendar.
`.claude/hooks/ask_before_writes.sh` makes Claude Code ask before each of them, and before
`curl` POSTs to the API's write endpoints or Python that calls the write functions.

## Skills
`.claude/skills/`: `morning-run` (run/verify today's autopilot), `diagnose-run` (why a run
failed), `add-profile` (onboard an athlete).
