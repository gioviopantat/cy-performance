"""Readiness job: assemble each day's inputs from a :class:`~cyp.dataset.Dataset`, score, persist.

Inputs per day *d* (morning-of semantics):

- wellness of *d* and the :data:`~cyp.analysis.readiness.BASELINE_DAYS` days before;
- TSB = form going into *d*: yesterday's ``tsb_icu``, else yesterday's ``tsb_sim``;
- yesterday's rides: worst status, hardest class, the longest ride's decoupling
  (+ reliability) and HR lag, the day's total load; the plan for yesterday is our
  ``planned_workouts.target_tss``, else icu ``WORKOUT`` events' ``icu_training_load``;
- an icu ``SICK`` / ``INJURED`` event covering *d*.

:func:`inputs_for` and :func:`readiness_for` are pure; :func:`run_readiness` loads the cached
snapshot once for any number of days and bulk-upserts ``readiness_daily``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.readiness import (
    BASELINE_DAYS,
    ReadinessInputs,
    WellnessPoint,
    YesterdayRide,
    compute_readiness,
)
from cyp.core.data_quality import DataQuality
from cyp.core.errors import AnalysisError
from cyp.core.load import Readiness
from cyp.dataset import CACHE, Dataset, WellnessRow
from cyp.store.models import ReadinessDaily

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


def _point(w: WellnessRow) -> WellnessPoint:
    return WellnessPoint(
        date=w.date,
        hrv=w.hrv,
        resting_hr=w.resting_hr,
        sleep_s=w.sleep_s,
        sleep_score=w.sleep_score,
        soreness=w.soreness,
        fatigue=w.fatigue,
        stress=w.stress,
        mood=w.mood,
        injury=w.injury,
    )


def _planned_load(ds: Dataset, day: dt.date) -> float | None:
    if day in ds.planned_load:
        return ds.planned_load[day]
    vals = [
        e.load
        for e in ds.events_between(day, day)
        if e.category == "WORKOUT" and not e.ours and e.load is not None
    ]
    return float(sum(vals)) if vals else None  # type: ignore[arg-type]


#: ``activity_id -> (rpe, feel)`` answered outside intervals.icu (web 騎完感受); wins over icu.
Feedback = Mapping[int, tuple[float | None, int | None]]


def _yesterday(ds: Dataset, day: dt.date, feedback: Feedback | None = None) -> YesterdayRide | None:
    """Yesterday's response. ``feedback`` is ``None`` when ``readiness.ride_feel`` is off."""
    y = day - dt.timedelta(days=1)
    rides = [a for a in ds.on(y) if a.is_ride and a.analysed]
    planned = _planned_load(ds, y)
    if not rides:
        if planned is None:
            return None
        return YesterdayRide(load=ds.loads.get(y, 0.0), planned_load=planned)
    longest = max(rides, key=lambda a: a.moving_s)
    # RPE / feel come from one ride (the longest one rated), and are judged against that ride's
    # own class and length. Per field, the web answer wins over intervals.icu's.
    rpe: float | None = None
    feel: int | None = None
    rated = None
    if feedback is not None:
        for a in sorted(rides, key=lambda r: r.moving_s, reverse=True):
            local = feedback.get(a.id, (None, None))
            r = local[0] if local[0] is not None else a.rpe
            f = local[1] if local[1] is not None else a.feel
            if r is not None or f is not None:
                rpe, feel, rated = r, f, a
                break
    statuses = [a.status for a in rides if a.status]
    classes = [a.classification for a in rides if a.classification]
    return YesterdayRide(
        status=max(statuses, key=lambda st: _STATUS_RANK.get(st, 1)) if statuses else None,
        classification=max(classes, key=lambda c: _CLASS_RANK.get(c, -1)) if classes else None,
        decoupling_pct=longest.decoupling_pct,
        decoupling_reliable=longest.decoupling_reliable,
        hr_lag_s=longest.hr_lag_s,
        load=ds.loads.get(y),
        planned_load=planned,
        rpe=rpe,
        feel=feel,
        rated_classification=rated.classification if rated else None,
        rated_minutes=rated.moving_s / 60 if rated and rated.moving_s else None,
    )


def _sick(ds: Dataset, day: dt.date) -> str | None:
    for e in ds.events:
        if e.category not in ("SICK", "INJURED") or e.date is None:
            continue
        if e.date <= day <= (e.end_date or e.date):
            return e.category
    return None


def _tsb(ds: Dataset, day: dt.date) -> float | None:
    row = ds.fitness.get(day - dt.timedelta(days=1))
    return row.tsb if row is not None else None


def inputs_for(ds: Dataset, day: dt.date, feedback: Feedback | None = None) -> ReadinessInputs:
    """Assemble :class:`ReadinessInputs` for ``day`` (pure; ``feedback`` None = RPE/feel off)."""
    lo = day - dt.timedelta(days=BASELINE_DAYS)
    history = [_point(w) for d, w in sorted(ds.wellness.items()) if lo <= d < day]
    today = ds.wellness.get(day)
    return ReadinessInputs(
        date=day,
        today=_point(today) if today else None,
        history=history,
        tsb=_tsb(ds, day),
        yesterday=_yesterday(ds, day, feedback),
        sick_or_injured=_sick(ds, day),
    )


def readiness_for(ds: Dataset, day: dt.date, feedback: Feedback | None = None) -> Readiness:
    """Score one day (pure). Pass ``feedback`` (may be empty) to use post-ride RPE / feel."""
    return compute_readiness(inputs_for(ds, day, feedback))


def persist(session: Session, athlete_id: int, verdicts: Iterable[Readiness]) -> None:
    """Upsert ``readiness_daily`` rows (one SELECT for all dates)."""
    verdicts = list(verdicts)
    if not verdicts:
        return
    existing = {
        r.date_local: r
        for r in session.scalars(
            select(ReadinessDaily).where(
                ReadinessDaily.athlete_id == athlete_id,
                ReadinessDaily.date_local.in_([v.date_local for v in verdicts]),
            )
        )
    }
    for r in verdicts:
        row = existing.get(r.date_local)
        if row is None:
            row = ReadinessDaily(athlete_id=athlete_id, date_local=r.date_local)
            session.add(row)
        row.score_0_100 = r.score_0_100
        row.status = r.status
        row.recommendation = r.recommendation
        row.inputs = r.inputs
        row.explanation = r.explanation.to_json_dict() if r.explanation else None
        row.algo_version = r.algo_version


def run_readiness(
    factory: sessionmaker[Session],
    days: Iterable[dt.date],
    *,
    data_quality: DataQuality | None = None,
    feedback: Feedback | None = None,
) -> list[Readiness]:
    """Compute and persist readiness for each day; returns the verdicts in order.

    ``feedback`` as in :func:`readiness_for` (``None`` = post-ride RPE / feel off).

    Raises:
        AnalysisError: no athlete in the store.
    """
    ds = CACHE.get(factory, data_quality=data_quality)
    if ds is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    out = [readiness_for(ds, d, feedback) for d in days]
    with factory() as s:
        persist(s, ds.athlete_id, out)
        s.commit()
    return out
