# cy-performance

Personal cycling performance backend: ingests ride data from **Strava** and **intervals.icu**,
analyses every ride and the athlete's long-term trends, and **writes an adaptive training plan
back to the intervals.icu calendar** on a daily cadence.

Single-athlete, unattended, backend-only for now (no UI). A frontend and multi-user support are
explicitly out of scope for v1 but the architecture leaves room for both.

## Status

**M0 Scaffold** done, **M1 Ingest** done (see [docs/06-roadmap.md](docs/06-roadmap.md)): `uv`
project, `cyp` CLI, settings, structlog, SQLite + Alembic schema, Parquet stream store,
`job_runs`, `cyp doctor`, CI; intervals.icu + Strava clients and sync jobs, the Strava ↔ icu
matcher, and the unified `cyp sync` / `cyp backfill` orchestration. M2 (`cyp analyze`) in progress.

| Doc | What it covers |
|-----|----------------|
| [docs/01-architecture.md](docs/01-architecture.md) | Goals, principles, components, runtime, deployment, config, observability |
| [docs/02-integrations.md](docs/02-integrations.md) | Strava + intervals.icu API facts, auth, limits, legal constraints, source-of-truth matrix, workout text format |
| [docs/03-data-model.md](docs/03-data-model.md) | Storage layout and every table |
| [docs/04-analysis-engine.md](docs/04-analysis-engine.md) | Per-ride and longitudinal metrics, what is ported from `strava-analyis` |
| [docs/05-training-engine.md](docs/05-training-engine.md) | Athlete model, periodization, daily adaptation loop, guardrails, publishing |
| [docs/06-roadmap.md](docs/06-roadmap.md) | Milestones M0 to M6 with acceptance criteria |
| [docs/07-explainability.md](docs/07-explainability.md) | How every metric, verdict and workout explains itself to the athlete (zh-TW), glossary of models |
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
  jobs/             job_run context manager; sync.py (unified sync/backfill); daily/weekly (M2+)
  llm/              optional coach narrative + plan review (never computes numbers)   (M5)
  settings.py       pydantic-settings (.env) + config/athlete.yaml models
  logging.py        structlog -> data/logs/cyp.jsonl + console
  cli.py            `cyp` entry point
  api/              FastAPI app (webhooks, read-only JSON for a future UI)    (M6)
tests/
docs/
config/athlete.yaml Athlete intent and planner knobs (goals, season, availability)
```

## Quickstart

```bash
uv sync                          # Python 3.12+; creates .venv and installs cyp
cp .env.example .env             # fill in INTERVALS_API_KEY (+ Strava app creds, optional)
uv run cyp init                  # create data/{streams,tokens,logs,reports}
uv run cyp db upgrade            # apply Alembic migrations to data/cyp.sqlite
uv run cyp auth strava           # OAuth via local callback (port 8721) ...
uv run cyp auth strava --import-from ~/strava-analyis/token.json   # ... or reuse the old token
uv run cyp backfill --days 365   # history: icu (month pages, resumable) -> match -> Strava -> match
uv run cyp sync                  # daily incremental: same order; Strava streams only as fallback
uv run cyp doctor                # settings (masked), DB + schema, athlete.yaml, Strava token +
                                 # rate budget, icu cursors, matcher stats, last unified sync
```

`cyp doctor` exits non-zero until the data dir exists, the schema is at head, `config/athlete.yaml`
validates and `INTERVALS_API_KEY` is set. Set `STRAVA_ENABLED=false` in `.env` to run without
Strava entirely (ADR-0003: icu is the primary source; Strava adds segments/PRs + fallback streams).

Per-source commands (each also runs the matcher afterwards):

```bash
uv run cyp sync intervals [--backfill-days N] [--no-streams] [--stage activities ...]
uv run cyp sync strava [--full] [--streams] [--no-wait] [--max-detail N]
uv run cyp sync --no-streams --no-wait      # unified; --no-wait exits 2 instead of sleeping on a Strava 429
```

Strava reads are budgeted: `STRAVA_MAX_DETAIL_FETCHES` (default 60) caps detail + fallback-stream
fetches per run; re-run to continue (cursors persist). Commands that arrive with later milestones
currently print `not implemented in M0` and exit 0:

```bash
uv run cyp analyze               # M2 (in progress)
uv run cyp plan                  # M4: show / apply the plan diff (CYP_PLAN_MODE=propose|apply)
uv run cyp daily                 # the unattended pipeline launchd/cron runs
uv run cyp weekly
uv run cyp explain <key>         # prints a persisted Explanation (docs/07); rendering lands in M5
```

## Development

```bash
uv sync --all-groups             # runtime + dev deps (pytest, ruff, mypy)
uv run pytest                    # tests use tmp data dirs + respx; nothing is written under data/, no network
uv run ruff check . && uv run ruff format --check .
uv run mypy src                  # strict mode; part of the gate

# schema changes: edit src/cyp/store/models.py, then
CYP_DB_URL=sqlite:////tmp/mig.sqlite uv run alembic upgrade head
CYP_DB_URL=sqlite:////tmp/mig.sqlite uv run alembic revision --autogenerate -m "describe change"
```

CI (`.github/workflows/ci.yml`) runs `uv sync`, `ruff check`, `ruff format --check` and `pytest`
on Python 3.12 and 3.13.
