# ADR-0001 · Python 3.12+, uv, CLI-first

**Status**: accepted · 2026-10-02

## Context
The athlete already has a working Python/uv/SQLite/pandas Strava pipeline (`../strava-analyis`,
~80 rides, tests, launchd scheduling, Chinese coaching reports). The new project adds
intervals.icu, longitudinal modelling and a planner. Node 20 and Bun are also installed.

## Decision
Python ≥ 3.12 managed by `uv`; `pydantic` v2 models; `httpx` clients; `polars` (with pandas interop)
for frames; `SQLAlchemy 2` + `Alembic`; `typer` CLI; `structlog`; `APScheduler` only inside
`cyp serve`; `FastAPI` optional. Package `cyp` under `src/`.

## Consequences
- Direct port of tested metric code; no rewrite tax.
- Scientific stack (numpy fits for CP/W′, Banister) is native.
- A future UI is a separate TypeScript project talking to the FastAPI JSON layer; no shared code needed.
