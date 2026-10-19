"""Trends recompute / latest report, and explanation lookup."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from cyp.analysis.longitudinal.run import build_trends, load_latest_report
from cyp.schemas import ExplainOut
from cyp.services.context import AppContext
from cyp.store.models import EXPLANATION_TABLES


def season_phase(ctx: AppContext, day: dt.date) -> str | None:
    """Season phase of ``day`` (``None`` outside the season / without config)."""
    from cyp.planning.season import build_skeleton

    cfg = ctx.athlete_config_or_none()
    if cfg is None:
        return None
    week = build_skeleton(cfg).week_of(day)
    return week.phase if week else None


def recompute(ctx: AppContext, *, as_of: dt.date | None = None) -> dict[str, Any]:
    """Run the trends job (persists fitness + curve snapshots + report JSON)."""
    day = as_of or ctx.today()
    with ctx.write_lock:
        report = build_trends(
            ctx.factory,
            ctx.store,
            data_quality=ctx.data_quality(),
            as_of=day,
            phase=season_phase(ctx, day),
            reports_dir=ctx.reports_dir,
        )
    return report.to_json()


def latest(ctx: AppContext) -> dict[str, Any] | None:
    """Last written trends report."""
    return load_latest_report(ctx.reports_dir)


def explain(ctx: AppContext, key: str) -> ExplainOut | None:
    """Find an Explanation by key in the explanation-bearing tables or the trends report.

    ``ride:<id>`` and ``readiness.<date>`` are primary-key lookups; other keys scan.
    """
    fast = _fast_lookup(ctx, key)
    if fast is not None:
        return fast
    with ctx.factory() as session:
        for model in EXPLANATION_TABLES:
            col = model.explanation  # type: ignore[attr-defined]
            for blob in session.scalars(select(col).where(col.is_not(None))):
                if isinstance(blob, dict) and blob.get("key") == key:
                    return ExplainOut(key=key, source=model.__tablename__, explanation=blob)
                items = blob.get("items") if isinstance(blob, dict) else None
                for item in items if isinstance(items, list) else []:
                    if isinstance(item, dict) and item.get("key") == key:
                        return ExplainOut(key=key, source=model.__tablename__, explanation=item)
    for blob in (latest(ctx) or {}).get("explanations", []):
        if isinstance(blob, dict) and blob.get("key") == key:
            return ExplainOut(key=key, source="reports/trends/latest.json", explanation=blob)
    return None


def _fast_lookup(ctx: AppContext, key: str) -> ExplainOut | None:
    from cyp.store.models import ActivityMetrics, ReadinessDaily

    with ctx.factory() as s:
        blob = None
        table = ""
        if key.startswith("ride:") and key[5:].isdigit():
            m = s.get(ActivityMetrics, int(key[5:]))
            blob, table = (m.explanation if m else None), "activity_metrics"
        elif key.startswith("readiness."):
            try:
                day = dt.date.fromisoformat(key.split(".", 1)[1])
            except ValueError:
                return None
            ds = ctx.dataset()
            r = s.get(ReadinessDaily, (ds.athlete_id, day))
            blob, table = (r.explanation if r else None), "readiness_daily"
        if isinstance(blob, dict) and blob.get("key") == key:
            return ExplainOut(key=key, source=table, explanation=blob)
    return None
