"""Adaptive planning inputs per profile: personal scales (spec adaptive-planning, Phase 1).

Flags decide here (ADR-0008), the pure code below never asks:

- :func:`scales_for`: the athlete's personal scales as of a day (``None`` with
  ``plan.personal_scales`` off), ``athlete.yaml planner.scales`` > learned > default.
- :func:`readiness_scales`: the readiness inputs those scales give (empty when off).
- :func:`adaptive_for`: what :func:`cyp.planning.job.plan_horizon` gets (empty when off, plus
  the athlete's casual-ride days, which no flag gates).
- :func:`record`: the morning run stores the scales in ``learned_params``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select

from cyp.analysis.personal_scales import SCALES_VERSION, PersonalScales, learn
from cyp.dataset import Dataset
from cyp.planning.adapt import easy_day_default_tss
from cyp.planning.athlete_rules import is_senior
from cyp.planning.gate import GateDecision, gate_chain
from cyp.planning.job import Adaptive
from cyp.planning.season import build_skeleton
from cyp.services.context import AppContext
from cyp.services.readiness import ride_feedback
from cyp.services.ride_feedback import casual_days
from cyp.store.models import LearnedParam

SCALES_FLAG = "plan.personal_scales"
GATE_FLAG = "plan.progression_gate"


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


def offsets_at(ctx: AppContext, ds: Dataset) -> Callable[[dt.date], Mapping[str, float]]:
    """Personal RPE offsets learned before a day, cached per day (``{}`` with scales off)."""
    if not ctx.features().enabled(SCALES_FLAG):
        return lambda _day: {}
    cfg = ctx.athlete_config_or_none()
    written = cfg.planner.scales.rpe_offset if cfg is not None else None
    feedback = ride_feedback(ctx)
    cache: dict[dt.date, Mapping[str, float]] = {}

    def at(day: dt.date) -> Mapping[str, float]:
        if day not in cache:
            cache[day] = learn(
                ds, day, feedback, easy_default_tss=None, rpe_offset=written
            ).offsets()
        return cache[day]

    return at


def readiness_inputs(ctx: AppContext, day: dt.date, ds: Dataset) -> dict[str, Any]:
    """Readiness keyword arguments for ``day`` (``{}`` with the flag off).

    The scales are learned before the rated ride (the day before), so a ride never sets its
    own baseline.
    """
    return readiness_scales(scales_for(ctx, day - dt.timedelta(days=1), ds))


def adaptive_for(
    scales: PersonalScales | None,
    *,
    gate: bool = False,
    offsets: Callable[[dt.date], Mapping[str, float]] | None = None,
    casual_days: frozenset[dt.date] = frozenset(),
) -> Adaptive:
    """Planner inputs from the scales, the gate flag and the athlete's casual-ride tags."""
    return Adaptive(
        easy_day=scales.easy_day_tss if scales is not None else None,
        gate=gate,
        rpe_offset_at=offsets if gate else None,
        casual_days=casual_days,
    )


def plan_inputs(ctx: AppContext, day: dt.date, ds: Dataset | None = None) -> Adaptive:
    """Everything adaptive the planner gets for ``day`` under this profile's flags."""
    data = ds if ds is not None else ctx.dataset()
    gate = ctx.features().enabled(GATE_FLAG)
    return adaptive_for(
        scales_for(ctx, day, data),
        gate=gate,
        offsets=offsets_at(ctx, data) if gate else None,
        casual_days=casual_days(ctx, data),
    )


def gate_decision(ctx: AppContext, week_start: dt.date) -> GateDecision | None:
    """The progression gate's decision for the week starting ``week_start`` (flag off: None).

    The same chain the planner runs (``planning/gate.gate_chain``), for the weekly digest's
    section on why next week is planned the way it is.
    """
    if not ctx.features().enabled(GATE_FLAG):
        return None
    cfg = ctx.athlete_config_or_none()
    if cfg is None:
        return None
    try:
        sk = build_skeleton(cfg)
    except ValueError:  # a season too short to plan has no gate
        return None
    ds = ctx.dataset()
    chain = gate_chain(
        ds,
        sk,
        cfg.availability.weekly_max_minutes,
        until=week_start,
        rpe_offset_at=offsets_at(ctx, ds),
        conservative=is_senior(cfg, week_start),
    )
    return chain.get(week_start)


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
