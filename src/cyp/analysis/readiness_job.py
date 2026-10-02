"""Readiness job: gather one day's inputs from the store, score, persist ``readiness_daily``.

Inputs per day *d* (morning-of semantics):

- wellness of *d* and the :data:`~cyp.analysis.readiness.BASELINE_DAYS` days before;
- TSB = form going into *d*: yesterday's ``tsb_icu``, else yesterday's ``tsb_sim``;
- yesterday's rides (``activity_metrics``): worst status, hardest class, the longest ride's
  decoupling (+ reliability) and HR lag, the day's total load; the plan for yesterday is
  ``planned_workouts.target_tss`` (ours), else icu ``WORKOUT`` events' ``icu_training_load``;
- an icu ``SICK`` / ``INJURED`` event covering *d*.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal.run import daily_loads, primary_athlete_id
from cyp.analysis.readiness import (
    BASELINE_DAYS,
    ReadinessInputs,
    WellnessPoint,
    YesterdayRide,
    compute_readiness,
)
from cyp.analysis.run import activity_local_date
from cyp.core.errors import AnalysisError
from cyp.core.load import Readiness
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    FitnessDaily,
    IcuEvent,
    PlannedWorkout,
    ReadinessDaily,
    WellnessDaily,
)

_STATUS_RANK = {"FRESH": 0, "NORMAL": 1, "BLUNTED": 2, "OVERREACHED": 3}
_CLASS_RANK = {
    "recovery": 0,
    "endurance": 1,
    "mixed": 2,
    "tempo": 3,
    "sweetspot": 4,
    "threshold": 5,
    "vo2": 6,
    "race": 7,
}


def _point(w: WellnessDaily) -> WellnessPoint:
    return WellnessPoint(
        date=w.date_local,
        hrv=w.hrv,
        resting_hr=w.resting_hr,
        sleep_s=float(w.sleep_s) if w.sleep_s is not None else None,
        sleep_score=w.sleep_score,
        soreness=_f(w.soreness),
        fatigue=_f(w.fatigue),
        stress=_f(w.stress),
        mood=_f(w.mood),
        injury=_f(w.injury),
    )


def _f(v: int | float | None) -> float | None:
    return float(v) if v is not None else None


def _yesterday(session: Session, athlete_id: int, day: dt.date) -> YesterdayRide | None:
    y = day - dt.timedelta(days=1)
    lo, hi = (y - dt.timedelta(days=1)).isoformat(), (y + dt.timedelta(days=2)).isoformat()
    rows = session.execute(
        select(Activity, ActivityMetrics)
        .join(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
        .where(Activity.is_ride.is_(True), Activity.start_utc >= lo, Activity.start_utc < hi)
    ).all()
    rides = [(a, m) for a, m in rows if activity_local_date(a) == y]
    loads, _ = daily_loads(session)
    planned = _planned_load(session, athlete_id, y)
    if not rides:
        if planned is None:
            return None
        return YesterdayRide(load=loads.get(y, 0.0), planned_load=planned)
    longest = max(rides, key=lambda am: am[0].moving_s or 0)[1]
    detail = longest.hr_drift_detail if isinstance(longest.hr_drift_detail, dict) else {}
    statuses = [m.status for _, m in rides if m.status]
    return YesterdayRide(
        status=max(statuses, key=lambda st: _STATUS_RANK.get(st, 1)) if statuses else None,
        classification=_classification(rides),
        decoupling_pct=longest.decoupling_pct,
        decoupling_reliable=bool(detail.get("reliable")),
        hr_lag_s=longest.hr_lag_s,
        load=loads.get(y),
        planned_load=planned,
    )


def _classification(rides: list[tuple[Activity, ActivityMetrics]]) -> str | None:
    """Hardest stored ride class (the pipeline keeps it in ``pacing['classification']``)."""
    found = [
        m.pacing["classification"]
        for _, m in rides
        if isinstance(m.pacing, dict) and isinstance(m.pacing.get("classification"), str)
    ]
    return max(found, key=lambda c: _CLASS_RANK.get(c, -1)) if found else None


def _planned_load(session: Session, athlete_id: int, day: dt.date) -> float | None:
    ours = session.scalars(
        select(PlannedWorkout.target_tss).where(
            PlannedWorkout.athlete_id == athlete_id,
            PlannedWorkout.date_local == day,
            PlannedWorkout.status.not_in(("cancelled", "superseded")),
        )
    ).all()
    vals = [v for v in ours if v is not None]
    if vals:
        return float(sum(vals))
    icu = session.scalars(
        select(IcuEvent.icu_training_load).where(
            IcuEvent.category == "WORKOUT",
            IcuEvent.start_date_local >= day.isoformat(),
            IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
        )
    ).all()
    vals = [v for v in icu if v is not None]
    return float(sum(vals)) if vals else None


def _sick(session: Session, day: dt.date) -> str | None:
    rows = session.scalars(
        select(IcuEvent).where(
            IcuEvent.category.in_(("SICK", "INJURED")),
            IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
        )
    ).all()
    for e in rows:
        start = (e.start_date_local or "")[:10]
        end = (e.end_date_local or e.start_date_local or "")[:10]
        if start and start <= day.isoformat() <= (end or start):
            return e.category
    return None


def _tsb(session: Session, athlete_id: int, day: dt.date) -> float | None:
    row = session.get(FitnessDaily, (athlete_id, day - dt.timedelta(days=1)))
    if row is None:
        return None
    return row.tsb_icu if row.tsb_icu is not None else row.tsb_sim


def inputs_for(session: Session, athlete_id: int, day: dt.date) -> ReadinessInputs:
    """Assemble :class:`ReadinessInputs` for ``day`` from the store."""
    rows = session.scalars(
        select(WellnessDaily)
        .where(
            WellnessDaily.athlete_id == athlete_id,
            WellnessDaily.date_local >= day - dt.timedelta(days=BASELINE_DAYS),
            WellnessDaily.date_local <= day,
        )
        .order_by(WellnessDaily.date_local)
    ).all()
    today = next((_point(w) for w in rows if w.date_local == day), None)
    history = [_point(w) for w in rows if w.date_local < day]
    return ReadinessInputs(
        date=day,
        today=today,
        history=history,
        tsb=_tsb(session, athlete_id, day),
        yesterday=_yesterday(session, athlete_id, day),
        sick_or_injured=_sick(session, day),
    )


def persist(session: Session, athlete_id: int, r: Readiness) -> None:
    """Upsert one ``readiness_daily`` row."""
    row = session.get(ReadinessDaily, (athlete_id, r.date_local))
    if row is None:
        row = ReadinessDaily(athlete_id=athlete_id, date_local=r.date_local)
        session.add(row)
    row.score_0_100 = r.score_0_100
    row.status = r.status
    row.recommendation = r.recommendation
    row.inputs = r.inputs
    row.explanation = r.explanation.to_json_dict() if r.explanation else None
    row.algo_version = r.algo_version


def run_readiness(factory: sessionmaker[Session], days: Iterable[dt.date]) -> list[Readiness]:
    """Compute and persist readiness for each day; returns the verdicts in order.

    Raises:
        AnalysisError: no athlete in the store.
    """
    out: list[Readiness] = []
    with factory() as s:
        athlete_id = primary_athlete_id(s)
        if athlete_id is None:
            raise AnalysisError("no athlete in the store; run `cyp sync` first")
        for day in days:
            r = compute_readiness(inputs_for(s, athlete_id, day))
            persist(s, athlete_id, r)
            out.append(r)
        s.commit()
    return out
