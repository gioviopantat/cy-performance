"""Plan compliance: fill ``planned_workouts.compliance`` for the last days (spec adaptive-planning).

The morning run (analysis stage) calls :func:`record` after readiness: every live planned slot
of ours in the :data:`COMPLIANCE_DAYS` days before today gets its plan-vs-actual record
(:func:`cyp.planning.compliance.compliance_record`); a ride synced late fixes the record the
next morning. :func:`judge_day` is the shared core, also used by ``cyp plan replay`` with a
replayed (never stored) planned workout. The intent verdict is the per-ride report's
(``reports/ride_intent.build_intent``) for the ride that carries the plan.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cyp.analysis.readiness_job import Feedback, ride_response
from cyp.analysis.ride.frames import RideFrame
from cyp.core.errors import NotFoundError
from cyp.dataset import ActivityRow, Dataset
from cyp.planning.compliance import compliance_record
from cyp.reports.facts import ride_fact
from cyp.reports.ride_common import RideInputs
from cyp.reports.ride_intent import build_intent
from cyp.services.context import AppContext
from cyp.services.readiness import ride_feedback
from cyp.services.ride_feedback import casual_ids
from cyp.settings import AthleteConfig
from cyp.store.models import Activity, ActivityMetrics, PlannedWorkout
from cyp.store.repo.planned import is_live
from cyp.store.streams import StreamStore

#: Days before today whose records are (re)written each morning (late syncs, edited RPE).
COMPLIANCE_DAYS = 7


def _carrier(ds: Dataset, day: dt.date, pw: PlannedWorkout) -> ActivityRow | None:
    """The ride the plan was executed on: the matched one, else the day's longest ride."""
    rides = [a for a in ds.on(day) if a.is_ride]
    for a in rides:
        if pw.executed_activity_id == a.id:
            return a
    return max(rides, key=lambda x: (x.moving_s, -x.id), default=None)


def intent_verdict(
    session: Session,
    store: StreamStore,
    ds: Dataset,
    cfg: AthleteConfig | None,
    row: ActivityRow,
    pw: PlannedWorkout,
    frames: dict[int, RideFrame | None] | None = None,
    casual: bool = False,
) -> str | None:
    """The intent verdict of ``row`` against ``pw`` (``None`` when the ride is not stored)."""
    a = session.get(Activity, row.id)
    if a is None:
        return None
    m = session.get(ActivityMetrics, row.id)
    inp = RideInputs(
        session=session,
        store=store,
        activity=a,
        metrics=m,
        fact=ride_fact(a, m),
        day=row.date,
        cfg=cfg,
        ds=ds,
        row=row,
        trends=None,
        _frames=frames if frames is not None else {},
        casual=casual,
    )
    try:
        intent = build_intent(inp, planned=(pw, None))
    except NotFoundError:
        return None
    return intent.verdict if intent is not None else None


def judge_day(
    session: Session,
    store: StreamStore,
    ds: Dataset,
    cfg: AthleteConfig | None,
    day: dt.date,
    pw: PlannedWorkout,
    feedback: Feedback | None,
    *,
    with_intent: bool = True,
    frames: dict[int, RideFrame | None] | None = None,
    casual: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    """Compliance record of planned workout ``pw`` on ``day`` (``pw`` may be transient).

    ``with_intent=False`` skips the stream-based intent check (``intent_verdict`` ``None``).
    ``casual``: rides the athlete tagged casual; a casual carrier makes the slot not done.
    """
    carrier = _carrier(ds, day, pw)
    is_casual = carrier is not None and carrier.id in casual
    response = ride_response(ds, day, feedback)
    steps = pw.steps if isinstance(pw.steps, dict) else {}
    verdict = (
        intent_verdict(session, store, ds, cfg, carrier, pw, frames, casual=is_casual)
        if carrier is not None and with_intent
        else "casual"
        if is_casual
        else None
    )
    return compliance_record(
        role=str(steps.get("role") or "hit"),
        intent=pw.intent,
        planned_tss=pw.target_tss,
        reference_tss=steps.get("reference_tss"),
        actual_tss=ds.loads.get(day, 0.0),
        rode=carrier is not None,
        intent_verdict=verdict,
        rpe=response.rpe if response is not None else None,
        feel=response.feel if response is not None else None,
        rated_class=response.rated_classification if response is not None else None,
        rated_minutes=response.rated_minutes if response is not None else None,
        casual=is_casual,
    )


def compute(ctx: AppContext, today: dt.date, days: int = COMPLIANCE_DAYS) -> dict[int, Any]:
    """``planned_workouts.id -> record`` for the live slots of the ``days`` days before today."""
    ds = ctx.dataset()
    cfg = ctx.athlete_config_or_none()
    feedback = ride_feedback(ctx)
    casual = casual_ids(ctx)
    frames: dict[int, RideFrame | None] = {}
    out: dict[int, Any] = {}
    with ctx.factory() as s:
        rows = s.scalars(
            select(PlannedWorkout)
            .where(
                PlannedWorkout.athlete_id == ds.athlete_id,
                PlannedWorkout.date_local >= today - dt.timedelta(days=days),
                PlannedWorkout.date_local < today,
                is_live(),
            )
            .order_by(PlannedWorkout.date_local, PlannedWorkout.slot)
        ).all()
        for pw in rows:
            out[pw.id] = judge_day(
                s, ctx.store, ds, cfg, pw.date_local, pw, feedback, frames=frames, casual=casual
            )
    return out


def record(ctx: AppContext, today: dt.date, days: int = COMPLIANCE_DAYS) -> int:
    """Write the records of :func:`compute`; returns how many slots changed."""
    records = compute(ctx, today, days)
    changed = 0
    with ctx.write_lock, ctx.factory() as s:
        for pw_id, rec in records.items():
            pw = s.get(PlannedWorkout, pw_id)
            if pw is not None and pw.compliance != rec:
                pw.compliance = rec
                changed += 1
        s.commit()
    return changed
