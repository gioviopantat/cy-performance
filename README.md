# cy-performance

Personal cycling performance backend: ingests ride data from **Strava** and **intervals.icu**,
analyses every ride and the athlete's long-term trends, and **writes an adaptive training plan
back to the intervals.icu calendar** on a daily cadence.

Unattended and backend-only (no UI): athletes see their plan in intervals.icu and on their head
unit. Several athletes run as isolated **profiles** on one always-on machine; every optional
capability is a **feature flag**. Agents: start at [CLAUDE.md](CLAUDE.md).

## Status

**M0 Scaffold** done, **M1 Ingest** done (see [docs/06-roadmap.md](docs/06-roadmap.md)): `uv`
project, `cyp` CLI, settings, structlog, SQLite + Alembic schema, Parquet stream store,
`job_runs`, `cyp doctor`, CI; intervals.icu + Strava clients and sync jobs, the Strava ↔ icu
matcher, and the unified `cyp sync` / `cyp backfill` orchestration. **M2 Analysis** done (per-ride
metrics, PMC replay vs icu, CP/W′, FTP proposals, durability, TID, repeat climbs, limiters,
readiness v1, daily/weekly zh-TW reports). **M3 Publish** done (live `events/bulk` upsert
spike 2026-10-03, first real plan applied 2026-10-06). **M4 Planner** v1 done (season skeleton,
week planning, guardrails, daily adaptation). **M4.5** done: profiles, feature flags and the
daily autopilot ([ADR-0006](docs/adr/0006-profiles-and-tenancy.md),
[ADR-0007](docs/adr/0007-ai-native-development.md), [ADR-0008](docs/adr/0008-feature-flags.md)).
**M5.5** web dashboard: buttons for today, plan, rides (RIDE.LOG, post-ride feeling, Strava
description) and runs; writes from the UI follow [ADR-0009](docs/adr/0009-writes-from-the-web-ui.md).
Container diagram: [docs/architecture.c4](docs/architecture.c4) (LikeC4).

| Doc | What it covers |
|-----|----------------|
| [docs/01-architecture.md](docs/01-architecture.md) | Goals, principles, components, runtime, deployment, config, observability |
| [docs/02-integrations.md](docs/02-integrations.md) | Strava + intervals.icu API facts, auth, limits, legal constraints, source-of-truth matrix, workout text format |
| [docs/03-data-model.md](docs/03-data-model.md) | Storage layout and every table |
| [docs/04-analysis-engine.md](docs/04-analysis-engine.md) | Per-ride and longitudinal metrics, what is ported from `strava-analyis` |
| [docs/05-training-engine.md](docs/05-training-engine.md) | Athlete model, periodization, daily adaptation loop, guardrails, publishing |
| [docs/06-roadmap.md](docs/06-roadmap.md) | Milestones M0 to M6 with acceptance criteria |
| [docs/07-explainability.md](docs/07-explainability.md) | How every metric, verdict and workout explains itself to the athlete (zh-TW), glossary of models |
| [docs/08-api.md](docs/08-api.md) | HTTP API behind the web dashboard |
| [docs/09-features.md](docs/09-features.md) | Feature flags: every switch, default and dependency |
| [docs/runbooks/](docs/runbooks/README.md) | What to do when the morning run fails |
| [docs/adr/](docs/adr/) | Architecture decision records |

## Layout

```
src/cyp/            Python package (see docs/01-architecture.md §5)
  core/             domain models, units, time, errors, Explanation (pydantic v2, no I/O)
  ingest/           strava/ and intervals/ API clients + sync jobs, matcher.py (Strava <-> icu)
  store/            SQLAlchemy models, Alembic migrations, Parquet StreamStore, repositories
  analysis/         per-ride metrics, longitudinal trends, readiness          (M2)
  planning/         athlete model, periodization, workout library, planner, guardrails (M4)
  publish/          intervals.icu calendar writer (idempotent upsert), plan diffs      (M3)
  jobs/             sync.py (unified sync/backfill), runner.py (cyp run, locks), launchd.py, notify.py
  llm/              optional coach narrative + plan review (never computes numbers)   (M5)
  services/         operations shared by CLI and API; autopilot.py, publish.py (write guard)
  profiles.py       one workspace per athlete under profiles/ (ADR-0006)
  settings.py       pydantic-settings (.env) + athlete.yaml models + profile/flag resolution
  logging.py        structlog -> data/logs/cyp.jsonl + console
  cli/              `cyp` entry point (core, sync, analysis, planning, reports, dev, serve,
                    profiles, autopilot, ridelog)
  api/              FastAPI app behind the web UI: JSON reads, what-ifs, plan preview/commit,
                    jobs, profiles, autopilot, ride logs
tests/
docs/
config/athlete.example.yaml  Format of athlete.yaml (invented numbers)
profiles/<name>/    PRIVATE, git-ignored: .env, athlete.yaml, profile.yaml, data/
```

## Quickstart

```bash
uv sync --all-groups             # Python 3.12+; creates .venv and installs cyp
uv run cyp profile add me        # asks for your intervals.icu API key (Settings -> Developer),
                                 # goal FTP and minutes per weekday; downloads 365 days of history
uv run cyp run --no-write        # sync -> analyse -> reports -> plan -> calendar diff
uv run cyp profile promote me    # let the autopilot write workouts to your calendar
uv run cyp schedule install      # macOS: run every profile daily at 05:30 (catches up on wake)
npm --prefix web ci && npm --prefix web run build   # once: build the dashboard
uv run cyp serve                 # http://127.0.0.1:8765 : today, plan, rides, runs; buttons
```

Another athlete (family, a friend): `uv run cyp profile add parent` with *their* API key. They
install nothing; their workouts appear in their intervals.icu calendar and on their Garmin
(enable "upload planned workouts" in their intervals.icu Garmin settings).

```bash
uv run cyp profile list                      # profiles, athlete ids, mode, last run
uv run cyp --profile parent features         # which stages run for them, and why
uv run cyp --profile parent run --json       # one run, machine-readable
uv run cyp run --all                         # what the scheduler runs
uv run cyp doctor                            # settings (masked), DB, athlete.yaml, tokens, last runs
uv run cyp profile adopt me                  # move a pre-profile setup (.env, data/) into profiles/
```

Strava is optional (`sync.strava` flag; ADR-0003: icu is primary). For the owner:
`uv run cyp --profile me auth strava` after adding `STRAVA_CLIENT_ID/SECRET` to the profile's `.env`,
then set `sync.strava: true` under `features:` in the profile's `athlete.yaml`.

New athlete? The questions to ask are in [docs/onboarding-questions.md](docs/onboarding-questions.md).

RIDE.LOG to Strava (flag `strava.write_description`, off by default): add `activity:write` to
`STRAVA_SCOPE` in the profile's `.env`, run `uv run cyp --profile me auth strava` once, set
`strava.write_description: true` under `features:`. Then `uv run cyp ride-log push latest`
previews and `--confirm-write` writes (or use the 行事曆 page: open the ride's day). Your own text on Strava is kept.
An edited RIDE.LOG (e.g. with the acrostic poem) is saved per ride (`cyp ride-log save`, or 儲存 on
the 行事曆 page, `data/ride_logs/<id>.txt`) and shown and pushed from then on.

Per-source commands (each also runs the matcher afterwards):

```bash
uv run cyp sync intervals [--backfill-days N] [--no-streams] [--stage activities ...]
uv run cyp sync strava [--full] [--streams] [--no-wait] [--max-detail N]
uv run cyp sync --no-streams --no-wait      # unified; --no-wait exits 2 instead of sleeping on a Strava 429
```

Strava reads are budgeted: `STRAVA_MAX_DETAIL_FETCHES` (default 60) caps detail + fallback-stream
fetches per run; re-run to continue (cursors persist).

```bash
uv run cyp sync refetch-streams              # re-download icu streams (lat/lng fix), then analyze
uv run cyp analyze [--rides-only]            # rides -> trends -> today's readiness
uv run cyp trends [--date D]                 # PMC vs icu, CP/W', FTP proposal, durability, TID, limiters
uv run cyp readiness [--date D] [--days N] [--coverage]
uv run cyp report daily|weekly [--date D]    # data/reports/{daily,weekly}/*.md (+ .json facts)
uv run cyp daily [--no-sync]                 # sync -> analyze -> trends -> readiness -> daily report
uv run cyp weekly [--no-sync]                # trends -> weekly report
uv run cyp plan [--date D] [--dry-run]       # 14-day horizon, stored as proposals (never writes icu)
uv run cyp plan --publish                    # + read-only diff against the live icu calendar
uv run cyp publish spike --date YYYY-MM-DD   # dry run; --confirm-write runs the upsert spike
uv run cyp plan --apply --confirm-write      # writes to icu (needs the spike result first)
uv run cyp explain <key>                     # e.g. readiness.2026-10-06, ftp.proposal, plan.day.2026-10-07
uv run cyp ftp [--what-if W] [--json]          # FTP evidence (~20 ms); `cyp ftp accept W --yes` records your decision
uv run cyp season                            # 26-week skeleton with targets and tests
uv run cyp dev seed --days 400               # synthetic demo athlete in an empty DB (no API keys)
uv run cyp dev bench                         # recompute latency (trends / FTP / readiness / plan)
uv run cyp serve                             # HTTP API for the frontend: http://127.0.0.1:8765/docs (docs/08-api.md)
uv run cyp dev openapi                       # docs/api/openapi.json for client generation
```

Nothing writes to the intervals.icu calendar except (a) `cyp plan --apply --confirm-write`, (b)
the autopilot for a profile promoted to `planner.mode: apply`, and (c) `cyp publish spike
--confirm-write` (one throw-away test event, deleted again). (a) and (b) pass the write guard (the
API key must belong to the profile's athlete), never write a plan that needs review, and only
touch events carrying our `external_id`.

## Development

```bash
uv sync --all-groups             # runtime + dev deps (pytest, ruff, mypy)
uv run poe check                 # THE gate (CI runs exactly this): ruff, format, mypy strict, pytest
uv run poe fmt                   # format + autofix

# schema changes: edit src/cyp/store/models.py, then
CYP_DB_URL=sqlite:////tmp/mig.sqlite uv run alembic upgrade head
CYP_DB_URL=sqlite:////tmp/mig.sqlite uv run alembic revision --autogenerate -m "describe change"
```

CI (`.github/workflows/ci.yml`) runs `uv run poe check` on Python 3.12 and 3.13. Tests never read
personal files: they use `tests/fixtures/athlete.reference.yaml` (an invented athlete).
