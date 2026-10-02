# ADR-0002 · SQLite + Parquet, SQLAlchemy abstraction

**Status**: accepted · 2026-10-02

## Context
One athlete, ~300 rides/year, ~50 MB/year including 1 Hz streams. Runs on a Mac via launchd today,
maybe a VPS later. Longitudinal analysis wants columnar scans over all streams.

## Decision
- Relational data in SQLite (WAL) through SQLAlchemy 2 + Alembic; no SQLite-specific SQL outside `store/`.
- Per-point streams as one Parquet file per activity, 1 Hz, fixed schema; polars `scan_parquet` for cross-ride scans.
- Raw API payloads kept as JSON columns.
- Backups: `data/` is a plain directory (Time Machine / Litestream when on a server).

## Alternatives rejected
- Postgres now: operational cost without benefit for one user; remains a URL change later.
- DuckDB as primary: great for analysis, weaker as an OLTP store for the planner's write path; can be added as a read engine over the same Parquet.
- Streams as JSON in SQLite (current project): fine at 24 MB, poor for longitudinal scans.
