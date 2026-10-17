"""Personal scales: how this athlete rates rides and what an easy day is for them.

Spec adaptive-planning, Phase 1 (flag ``plan.personal_scales``, ADR-0019). Descriptive values
learned from the athlete's own history, each with a minimum amount of data, shrinkage towards
the default and hard bounds (review §4: learning moves towards the athlete's baseline, never
towards "harder"):

- ``rpe_offset[class]``: median of (RPE − the class's table value, ``readiness.EXPECTED_RPE``
  with the long-ride allowance) over the last :data:`RPE_WINDOW_DAYS` days; a class with fewer
  than :data:`RPE_MIN_PER_CLASS` rated rides uses the all-class median; shrunk
  ``n·median / (n + k)`` with ``k`` = :data:`RPE_SHRINK_K`, clipped ±:data:`RPE_OFFSET_MAX`.
  Readiness compares RPE with table + offset (dad rates every Z2 a 5: his Z2 is expected ≈ 5).
- ``feel_baseline``: median feel (icu 1 strong … 5 weak) over :data:`FEEL_WINDOW_DAYS` days
  (≥ :data:`FEEL_MIN_N`), clipped to [1, 4]. A feel counts as bad only when it is at least
  ``readiness.FEEL_BAD_MARGIN`` worse than the baseline (never stricter than the fixed rule).
- ``easy_day_tss``: median load of ridden non-hard days over :data:`EASY_WINDOW_DAYS` days
  (≥ :data:`EASY_MIN_DAYS`), shrunk to the template default with ``k`` = :data:`EASY_SHRINK_K`
  and kept within default × [1, :data:`EASY_MAX_FACTOR`] (decision D2).

Priority per value: ``athlete.yaml planner.scales`` > learned > default. Every value carries
``n`` and its source so reports can say where it came from. Pure; RPE / feel come from
intervals.icu or the web answer (per field the web wins, as in readiness).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from cyp.analysis.readiness import EXPECTED_RPE, HARD_CLASSES, expected_rpe_for
from cyp.analysis.readiness_job import Feedback
from cyp.dataset import ActivityRow, Dataset

SCALES_VERSION = "personal_scales_v1"
#: Rated rides within this many days count for the RPE offsets.
RPE_WINDOW_DAYS = 90
#: A class needs this many rated rides for its own offset, else the all-class offset applies.
RPE_MIN_PER_CLASS = 5
#: Shrinkage towards 0: with n rides the offset is n·median / (n + k).
RPE_SHRINK_K = 5
#: An offset never exceeds this many RPE points either way.
RPE_OFFSET_MAX = 2.0
#: Rated rides within this many days count for the feel baseline.
FEEL_WINDOW_DAYS = 90
#: Feel answers needed before the baseline replaces the scale's middle.
FEEL_MIN_N = 8
#: icu feel's neutral middle (1 strong … 3 normal … 5 weak).
FEEL_DEFAULT = 3.0
#: Ridden days within this many days count for the easy-day load.
EASY_WINDOW_DAYS = 28
#: Easy days needed before the athlete's habit replaces the template default.
EASY_MIN_DAYS = 6
#: Shrinkage towards the template default: (n·median + k·default) / (n + k).
EASY_SHRINK_K = 5
#: The learned easy day stays within default × [1, this].
EASY_MAX_FACTOR = 1.8

Source = Literal["yaml", "learned", "default"]
ALL = "all"


@dataclass(frozen=True)
class Learned:
    """One value, how many observations back it, and where it came from."""

    value: float
    n: int
    source: Source
    #: The raw median before shrinkage and bounds (``None`` without data).
    raw: float | None = None


@dataclass(frozen=True)
class PersonalScales:
    """Every personal scale as of a day (rides before ``as_of`` only)."""

    as_of: dt.date
    rpe_offset: Mapping[str, Learned] = field(default_factory=dict)
    feel_baseline: Learned = Learned(FEEL_DEFAULT, 0, "default")
    easy_day_tss: Learned | None = None

    def offset_for(self, classification: str | None) -> float:
        """The RPE offset of ``classification`` (the all-class one for an unknown class)."""
        got = self.rpe_offset.get(classification or "") or self.rpe_offset.get(ALL)
        return got.value if got is not None else 0.0

    def offsets(self) -> dict[str, float]:
        """``class -> offset`` for every known class plus :data:`ALL` (readiness input)."""
        return {c: self.offset_for(c) for c in (*EXPECTED_RPE, ALL)}

    def rows(self) -> list[tuple[str, float, int, Source]]:
        """``(param, value, n, source)`` per value, for ``learned_params``."""
        out = [(f"rpe_offset.{c}", v.value, v.n, v.source) for c, v in self.rpe_offset.items()]
        fb = self.feel_baseline
        out.append(("feel_baseline", fb.value, fb.n, fb.source))
        if self.easy_day_tss is not None:
            e = self.easy_day_tss
            out.append(("easy_day_tss", e.value, e.n, e.source))
        return out


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _rated(
    ds: Dataset, as_of: dt.date, days: int, feedback: Feedback | None
) -> Iterable[tuple[ActivityRow, float | None, int | None]]:
    lo = as_of - dt.timedelta(days=days)
    for a in ds.activities:
        if not (a.is_ride and a.analysed and lo <= a.date < as_of):
            continue
        web = (feedback or {}).get(a.id, (None, None))
        rpe = web[0] if web[0] is not None else a.rpe
        feel = web[1] if web[1] is not None else a.feel
        if rpe is not None or feel is not None:
            yield a, (float(rpe) if rpe is not None else None), feel


def learn_rpe_offsets(
    ds: Dataset,
    as_of: dt.date,
    feedback: Feedback | None,
    configured: Mapping[str, float] | None = None,
) -> dict[str, Learned]:
    """``class -> offset`` (plus :data:`ALL`), see the module docstring."""
    deltas: dict[str, list[float]] = {}
    for a, rpe, _ in _rated(ds, as_of, RPE_WINDOW_DAYS, feedback):
        if rpe is None or a.classification not in EXPECTED_RPE:
            continue
        minutes = a.moving_s / 60 if a.moving_s else None
        delta = rpe - expected_rpe_for(a.classification, minutes)
        deltas.setdefault(a.classification, []).append(delta)
    every = [d for ds_ in deltas.values() for d in ds_]

    def shrunk(vals: list[float]) -> Learned:
        med = statistics.median(vals)
        v = len(vals) * med / (len(vals) + RPE_SHRINK_K)
        return Learned(
            round(_clip(v, -RPE_OFFSET_MAX, RPE_OFFSET_MAX), 2), len(vals), "learned", med
        )

    overall = (
        shrunk(every) if len(every) >= RPE_MIN_PER_CLASS else Learned(0.0, len(every), "default")
    )
    out: dict[str, Learned] = {ALL: overall}
    for cls in EXPECTED_RPE:
        vals = deltas.get(cls, [])
        out[cls] = shrunk(vals) if len(vals) >= RPE_MIN_PER_CLASS else overall
    for cls, v in (configured or {}).items():
        out[cls] = Learned(_clip(float(v), -RPE_OFFSET_MAX, RPE_OFFSET_MAX), 0, "yaml")
    return out


def learn_feel_baseline(
    ds: Dataset, as_of: dt.date, feedback: Feedback | None, configured: float | None = None
) -> Learned:
    """Median feel, see the module docstring."""
    if configured is not None:
        return Learned(_clip(float(configured), 1.0, 4.0), 0, "yaml")
    feels = [float(f) for _, _, f in _rated(ds, as_of, FEEL_WINDOW_DAYS, feedback) if f]
    if len(feels) < FEEL_MIN_N:
        return Learned(FEEL_DEFAULT, len(feels), "default")
    med = statistics.median(feels)
    return Learned(_clip(med, 1.0, 4.0), len(feels), "learned", med)


def easy_days(ds: Dataset, as_of: dt.date) -> list[tuple[dt.date, float]]:
    """``(day, load)`` of ridden non-hard days in the :data:`EASY_WINDOW_DAYS` before ``as_of``."""
    lo = as_of - dt.timedelta(days=EASY_WINDOW_DAYS)
    by_day: dict[dt.date, list[ActivityRow]] = {}
    for a in ds.activities:
        if a.is_ride and lo <= a.date < as_of:
            by_day.setdefault(a.date, []).append(a)
    return [
        (d, ds.loads.get(d, 0.0))
        for d, rides in sorted(by_day.items())
        if not any(r.classification in HARD_CLASSES for r in rides) and ds.loads.get(d, 0.0) > 0
    ]


def learn_easy_day_tss(
    ds: Dataset, as_of: dt.date, default_tss: float, configured: float | None = None
) -> Learned:
    """The athlete's easy-day load (decision D2), see the module docstring."""
    if configured is not None:
        return Learned(float(configured), 0, "yaml")
    loads = [v for _, v in easy_days(ds, as_of)]
    if len(loads) < EASY_MIN_DAYS:
        return Learned(round(default_tss, 1), len(loads), "default")
    med = statistics.median(loads)
    n = len(loads)
    v = (n * med + EASY_SHRINK_K * default_tss) / (n + EASY_SHRINK_K)
    v = _clip(v, default_tss, EASY_MAX_FACTOR * default_tss)
    return Learned(round(v, 1), n, "learned", round(med, 1))


def learn(
    ds: Dataset,
    as_of: dt.date,
    feedback: Feedback | None,
    *,
    easy_default_tss: float | None,
    rpe_offset: Mapping[str, float] | None = None,
    feel_baseline: float | None = None,
    easy_day_tss: float | None = None,
) -> PersonalScales:
    """Every scale as of ``as_of``; the keyword values are ``athlete.yaml`` overrides."""
    return PersonalScales(
        as_of=as_of,
        rpe_offset=learn_rpe_offsets(ds, as_of, feedback, rpe_offset),
        feel_baseline=learn_feel_baseline(ds, as_of, feedback, feel_baseline),
        easy_day_tss=(
            learn_easy_day_tss(ds, as_of, easy_default_tss, easy_day_tss)
            if easy_default_tss is not None
            else None
        ),
    )
