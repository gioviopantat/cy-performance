"""Composite operations shared by ``cyp daily`` / ``cyp weekly`` and API jobs."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any

from cyp.analysis.run import analyze_pending
from cyp.services import readiness as readiness_service
from cyp.services import reports as reports_service
from cyp.services import trends as trends_service
from cyp.services.context import AppContext


def run_sync_job(
    ctx: AppContext, *, backfill_days: int | None = None, no_wait: bool = True
) -> dict[str, Any]:
    """Unified intervals.icu -> matcher -> Strava -> matcher sync."""
    from cyp.jobs.sync import run_sync

    with ctx.write_lock:
        result = run_sync(
            ctx.settings, backfill_days=backfill_days, no_wait=no_wait, factory=ctx.factory
        )
    return asdict(result)


def analyze(ctx: AppContext, *, force: bool = False, limit: int | None = None) -> dict[str, int]:
    """Per-ride analysis of the queue."""
    with ctx.write_lock:
        summary = analyze_pending(
            ctx.factory,
            store=ctx.store,
            force=force,
            limit=limit,
            log_path=str(ctx.settings.logs_dir / "cyp.jsonl"),
        )
    return summary.counts()


def daily(ctx: AppContext, *, day: dt.date | None = None, sync: bool = False) -> dict[str, Any]:
    """Sync (optional) -> analyze -> trends -> readiness -> daily report."""
    today = day or ctx.today()
    out: dict[str, Any] = {}
    if sync and ctx.settings.intervals_api_key.get_secret_value():
        out["sync"] = run_sync_job(ctx)
    out["analyze"] = analyze(ctx)
    trends = trends_service.recompute(ctx, as_of=today)
    out["trends"] = {"limiters": [lim["id"] for lim in trends.get("limiters", [])]}
    out["readiness"] = readiness_service.recompute(ctx, [today])[0].model_dump(mode="json")
    out["report"] = reports_service.build(ctx, "daily", today).stem
    return out


def weekly(ctx: AppContext, *, day: dt.date | None = None) -> dict[str, Any]:
    """Trends -> weekly report of the ISO week containing ``day`` (default: yesterday)."""
    today = ctx.today()
    trends_service.recompute(ctx, as_of=today)
    report = reports_service.build(ctx, "weekly", day or today - dt.timedelta(days=1))
    return {"report": report.stem}
