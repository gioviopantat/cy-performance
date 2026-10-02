"""Readiness: stored verdicts, on-the-fly scoring, recompute."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from cyp.analysis.readiness import COMPONENT_ZH
from cyp.analysis.readiness_job import persist, readiness_for
from cyp.core.explain import Explanation
from cyp.core.load import Readiness
from cyp.reports.render import REC_ZH
from cyp.schemas import ReadinessComponentOut, ReadinessOut
from cyp.services.context import AppContext
from cyp.store.models import ReadinessDaily


def _out(r: Readiness) -> ReadinessOut:
    comps = r.inputs.get("components", {})
    total = sum(float(c.get("weight", 0)) for c in comps.values()) or 1.0
    return ReadinessOut(
        date=r.date_local,
        score=r.score_0_100,
        status=r.status,
        recommendation=r.recommendation,
        recommendation_zh=REC_ZH.get(r.recommendation, r.recommendation),
        components=[
            ReadinessComponentOut(
                name=k,
                name_zh=COMPONENT_ZH.get(k, k),
                z=float(c["z"]),
                share=round(float(c.get("weight", 0)) / total, 3),
            )
            for k, c in sorted(comps.items(), key=lambda kv: -abs(kv[1].get("z") or 0))
        ],
        missing=list(r.inputs.get("missing", [])),
        rule_hits=list(r.inputs.get("rule_hits", [])),
        explanation=r.explanation,
    )


def get(ctx: AppContext, day: dt.date) -> ReadinessOut:
    """Stored verdict for ``day``; computed on the fly (not stored) when there is none."""
    ds = ctx.dataset()
    with ctx.factory() as s:
        row = s.get(ReadinessDaily, (ds.athlete_id, day))
        if row is not None and row.score_0_100 is not None:
            return _out(
                Readiness(
                    date_local=day,
                    score_0_100=row.score_0_100,
                    status=row.status,
                    recommendation=row.recommendation,
                    inputs=row.inputs or {},
                    explanation=Explanation.from_json_dict(row.explanation)
                    if row.explanation
                    else None,
                    algo_version=row.algo_version or "readiness_v1",
                )
            )
    return _out(readiness_for(ds, day))


def history(ctx: AppContext, start: dt.date, end: dt.date) -> list[ReadinessOut]:
    """Stored verdicts in ``[start, end]`` (days without one are omitted)."""
    ds = ctx.dataset()
    with ctx.factory() as s:
        rows = s.scalars(
            select(ReadinessDaily)
            .where(
                ReadinessDaily.athlete_id == ds.athlete_id,
                ReadinessDaily.date_local >= start,
                ReadinessDaily.date_local <= end,
            )
            .order_by(ReadinessDaily.date_local)
        ).all()
        return [
            _out(
                Readiness(
                    date_local=r.date_local,
                    score_0_100=r.score_0_100 or 0.0,
                    status=r.status or "NORMAL",  # type: ignore[arg-type]
                    recommendation=r.recommendation or "AS_PLANNED",  # type: ignore[arg-type]
                    inputs=r.inputs or {},
                    explanation=Explanation.from_json_dict(r.explanation)
                    if r.explanation
                    else None,
                )
            )
            for r in rows
            if r.score_0_100 is not None
        ]


def recompute(ctx: AppContext, days: list[dt.date]) -> list[ReadinessOut]:
    """Score and store each day."""
    ds = ctx.dataset()
    verdicts = [readiness_for(ds, d) for d in days]
    with ctx.write_lock, ctx.factory() as s:
        persist(s, ds.athlete_id, verdicts)
        s.commit()
    return [_out(v) for v in verdicts]
