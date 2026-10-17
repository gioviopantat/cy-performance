"""Adaptive planning inputs per profile: personal scales (spec adaptive-planning, Phase 1).

Flags decide here (ADR-0008), the pure code below never asks:

- :func:`scales_for`: the athlete's personal scales as of a day (``None`` with
  ``plan.personal_scales`` off), ``athlete.yaml planner.scales`` > learned > default.
- :func:`readiness_scales`: the readiness inputs those scales give (empty when off).
- :func:`adaptive_for`: what :func:`cyp.planning.job.plan_horizon` gets (empty when off).
- :func:`record`: the morning run stores the scales in ``learned_params``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from cyp.analysis.personal_scales import SCALES_VERSION, PersonalScales, learn
from cyp.dataset import Dataset
from cyp.planning.adapt import easy_day_default_tss
from cyp.planning.job import Adaptive
from cyp.services.context import AppContext
from cyp.services.readiness import ride_feedback
from cyp.store.models import LearnedParam

SCALES_FLAG = "plan.personal_scales"


def scales_for(ctx: AppContext, day: dt.date, ds: Dataset | None = None) -> PersonalScales | None:
    """Personal scales learned from rides before ``day`` (``None`` when the flag is off)."""
    if not ctx.features().enabled(SCALES_FLAG):
        return None
    cfg = ctx.athlete_config_or_none()
    written = cfg.planner.scales if cfg is not None else None
    return learn(
        ds if ds is not None else ctx.dataset(),
        day,
        ride_feedback(ctx),
        easy_default_tss=easy_day_default_tss(),
        rpe_offset=written.rpe_offset if written is not None else None,
        feel_baseline=written.feel_baseline if written is not None else None,
        easy_day_tss=written.easy_day_tss if written is not None else None,
    )


def readiness_scales(scales: PersonalScales | None) -> dict[str, Any]:
    """Keyword arguments for ``readiness_for`` (``{}`` = the fixed table and rule)."""
    if scales is None:
        return {}
    fb = scales.feel_baseline
    return {
        "rpe_offset": scales.offsets(),
        "feel_baseline": fb.value if fb.source != "default" else None,
    }


def adaptive_for(scales: PersonalScales | None) -> Adaptive:
    """Planner inputs from the scales (an empty :class:`Adaptive` when off)."""
    return Adaptive(easy_day=scales.easy_day_tss if scales is not None else None)


def plan_inputs(ctx: AppContext, day: dt.date, ds: Dataset | None = None) -> Adaptive:
    """Everything adaptive the planner gets for ``day`` under this profile's flags."""
    return adaptive_for(scales_for(ctx, day, ds))


def record(ctx: AppContext, today: dt.date) -> int:
    """Upsert today's scales into ``learned_params``; returns the rows written (0 when off)."""
    ds = ctx.dataset()
    scales = scales_for(ctx, today, ds)
    if scales is None:
        return 0
    rows = scales.rows()
    with ctx.write_lock, ctx.factory() as s:
        existing = {
            r.param: r
            for r in s.scalars(select(LearnedParam).where(LearnedParam.athlete_id == ds.athlete_id))
        }
        for param, value, n, source in rows:
            row = existing.get(param)
            if row is None:
                row = LearnedParam(athlete_id=ds.athlete_id, param=param)
                s.add(row)
            row.value, row.n, row.source = value, n, source
            row.as_of, row.algo_version = today, SCALES_VERSION
        s.commit()
    return len(rows)
