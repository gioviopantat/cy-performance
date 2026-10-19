"""v1 intent check: did a ride deliver the planned stimulus (docs/specs/ride-report.md §2)?

Pure. The planner (layer 5) is not imported: the caller resolves the planned workout and passes
an :class:`IntentTarget` in watts. Outdoor interruptions are *excused*, not failures.

Two kinds of target:

- ``work`` (tempo, sweet spot, threshold, VO2, …): the work steps' power band. Achieved
  minutes are moving seconds whose smoothed power lies in ``[lo, hi × OVERSHOOT_FRAC]``.
  A *rep* is a block of runs of smoothed power ≥ ``lo``. Two runs belong to one block when the
  gap between them is a *dip* (≤ :data:`EXCUSE_MAX_S` of easier pedalling: a short descent,
  traffic, a shift; it counts as rep time, so it pulls the block's mean down) or an *excused*
  interruption (≤ :data:`EXCUSE_MAX_S`, mostly stopped, coasting or descending; listed, and
  not counted as rep time). Gaps are only bridged while the block is still short of one planned
  rep, so planned rests never join two reps. A block counts when its mean power over the rep
  time is ≥ ``lo`` and the rep time holds :data:`REP_COUNT_FRAC` of the planned rep length (a
  24-min block counts as two 12-min reps). A block with that mean holding at least
  :data:`PARTIAL_REP_FRAC` of a rep is not a rep, but its minutes count toward the score.
- ``ceiling`` (recovery, endurance): the point is to stay easy for the planned time. Achieved
  minutes are moving seconds whose 90 s power (:data:`CEILING_SMOOTH_S`) is at or below
  ``hi × OVERSHOOT_FRAC`` (coasting counts: descents are part of an easy road ride); time above
  the ceiling is what fails it. With a ``floor_w`` (endurance), an NP under it is too easy to
  be the planned ride and caps the verdict at partial.

Score: achieved / planned minutes in the band (the spec's v1 measure); for rep workouts the
largest of that, reps counted / planned, and rep minutes / planned minutes (blocks whose mean
is in the band, partial ones from :data:`PARTIAL_REP_FRAC` of a rep, capped at the plan),
because a block's mean already holds the band and second-by-second dips on a road would
otherwise hide a delivered stimulus (Roger, 2026-10-19: a 16-min sweet spot at 234 W split by
one 36 s ease-off, and an 11-min set cut short by heat, scored as "missed 0/3"). Verdict
thresholds are the named constants below. Load ratio (actual / planned load) is
reported alongside; it decides the verdict only when the plan has no power band (e.g. an
intervals.icu workout without our template).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from cyp.analysis.ride.frames import BoolArray, FloatArray, runs

INTENT_VERSION = "ride_intent_v1.1"

IntentKind = Literal["work", "ceiling"]
IntentVerdict = Literal["achieved", "partial", "missed", "no_data"]

#: Smoothing window (s) for power, as for NP: one-second spikes and dips are not the stimulus.
SMOOTH_S = 30
#: Smoothing never exceeds a quarter of a rep, so 30/15 or 40/20 reps stay visible.
SMOOTH_REP_FRACTION = 0.25
#: Up to 10 % over the band top still delivers the planned stimulus (and a little more).
OVERSHOOT_FRAC = 1.10
#: A rep counts once it holds 80 % of its planned length (Coggan-style compliance on work steps).
REP_COUNT_FRAC = 0.80
#: A rep cut short still counts its minutes toward the score from this share of a rep.
PARTIAL_REP_FRAC = 0.50
#: The longest dip or interruption inside one rep (a long red light).
EXCUSE_MAX_S = 120
#: An interruption is excused when at least half of it was stopped, coasting or descending.
EXCUSE_SHARE = 0.5
#: Grade (%) at or below which a second counts as descending.
DESCENT_GRADE_PCT = -2.0
#: Score (achieved / planned) from which the stimulus counts as reached.
ACHIEVED_FRAC = 0.90
#: Score from which it counts as partly reached; below is missed.
PARTIAL_FRAC = 0.50
#: Ceiling targets are judged on 90 s power: a 30-60 s rise on rolling roads is terrain, a
#: climb held above the cap for minutes is the ride getting harder than planned.
CEILING_SMOOTH_S = 90
#: Ceiling targets: share of moving time above the ceiling tolerated as achieved / as partial.
CEILING_OVER_OK = 0.10
CEILING_OVER_MAX = 0.25


@dataclass(frozen=True)
class IntentTarget:
    """The planned stimulus in watts (resolved by the caller from the planned workout)."""

    kind: IntentKind
    lo_w: float
    hi_w: float
    target_s: int
    rep_s: int | None = None
    reps: int | None = None
    #: Endurance floor: an NP below it is too easy to be the planned ride (at most partial).
    floor_w: float | None = None


@dataclass
class Span:
    """A stretch of the ride in seconds from the start; ``reason`` for excused spans."""

    start_s: int
    end_s: int
    reason: str = ""


@dataclass
class IntentFacts:
    """Result of :func:`assess_intent`."""

    achieved_s: int = 0
    above_s: int = 0
    moving_s: int = 0
    reps_counted: int | None = None
    #: Seconds of blocks whose mean held the band, partial reps included (capped at the plan).
    rep_s_done: int = 0
    counted: list[Span] = field(default_factory=list)
    excused: list[Span] = field(default_factory=list)
    score: float | None = None
    #: NP under ``target.floor_w`` (ceiling targets with a floor).
    too_easy: bool = False
    verdict: IntentVerdict = "no_data"


def smooth(watts: FloatArray, window: int) -> FloatArray:
    """Centred moving average with NaN as 0 W (paused seconds pull the average down)."""
    x = np.nan_to_num(np.asarray(watts, dtype=np.float64), nan=0.0)
    w = max(1, min(int(window), x.size))
    kernel = np.ones(w) / w
    return np.asarray(np.convolve(x, kernel, mode="same"), dtype=np.float64)


def smoothing_window(target: IntentTarget) -> int:
    """Power smoothing (s) used for ``target`` (see :data:`SMOOTH_REP_FRACTION`)."""
    if target.kind == "ceiling":
        return CEILING_SMOOTH_S
    if target.rep_s:
        return max(3, min(SMOOTH_S, int(target.rep_s * SMOOTH_REP_FRACTION)))
    return SMOOTH_S


def _gap_reason(
    lo: int, hi: int, moving: BoolArray, watts: FloatArray, grade: FloatArray | None
) -> str | None:
    """``"stop"`` / ``"coast"`` when the gap ``[lo, hi)`` is excusable, else ``None``."""
    n = hi - lo
    if n <= 0 or n > EXCUSE_MAX_S:
        return None
    stopped = ~moving[lo:hi]
    w = watts[lo:hi]
    coasting = moving[lo:hi] & (np.nan_to_num(w, nan=0.0) <= 0.0)
    descending = np.zeros(n, dtype=bool)
    if grade is not None:
        descending = moving[lo:hi] & (np.nan_to_num(grade[lo:hi], nan=0.0) <= DESCENT_GRADE_PCT)
    if np.count_nonzero(stopped | coasting | descending) < EXCUSE_SHARE * n:
        return None
    return (
        "stop" if np.count_nonzero(stopped) >= np.count_nonzero(coasting | descending) else "coast"
    )


def _blocks(
    p: FloatArray,
    watts: FloatArray,
    moving: BoolArray,
    grade: FloatArray | None,
    lo_w: float,
    window: int,
    rep_s: int,
) -> list[tuple[list[tuple[int, int]], list[Span]]]:
    """Runs of ``p ≥ lo_w`` merged across dips and excused gaps (indices, not seconds).

    A gap shorter than the smoothing window is noise and always merges. Longer dips and excused
    interruptions merge only while the block is still short of one counted rep: after that
    rep the gap is the planned rest (30/15, over-under recoveries), not an interruption.
    Returns ``(runs, excused gaps)`` per block; gap spans hold grid indices.
    """
    blocks: list[tuple[list[tuple[int, int]], list[Span]]] = []
    for r in runs(p >= lo_w):
        if blocks:
            parts, gaps = blocks[-1]
            prev_end = parts[-1][1]
            gap = r[0] - prev_end
            on_s = parts[-1][1] - parts[0][0] - sum(g.end_s - g.start_s for g in gaps)
            if gap <= window:
                parts.append(r)
                continue
            if on_s < REP_COUNT_FRAC * rep_s:
                reason = _gap_reason(prev_end, r[0], moving, watts, grade)
                if reason is not None:
                    parts.append(r)
                    gaps.append(Span(prev_end, r[0], reason))
                    continue
                if gap <= EXCUSE_MAX_S:  # a dip: rep time, judged by the block's mean
                    parts.append(r)
                    continue
        blocks.append(([r], []))
    return blocks


def count_reps(n_on_s: int, rep_s: int) -> int:
    """Reps a block of ``n_on_s`` on-target seconds is worth (see :data:`REP_COUNT_FRAC`)."""
    return max(0, math.floor(n_on_s / rep_s + (1.0 - REP_COUNT_FRAC) + 1e-9))


def verdict_from_score(score: float) -> IntentVerdict:
    """Achieved at ≥ :data:`ACHIEVED_FRAC`, partial at ≥ :data:`PARTIAL_FRAC`, else missed."""
    if score >= ACHIEVED_FRAC:
        return "achieved"
    if score >= PARTIAL_FRAC:
        return "partial"
    return "missed"


def _ceiling(
    target: IntentTarget, p: FloatArray, moving: BoolArray, np_w: float | None, out: IntentFacts
) -> IntentFacts:
    ceiling = target.hi_w * OVERSHOOT_FRAC
    out.achieved_s = int(np.count_nonzero(moving & (p <= ceiling)))
    out.above_s = int(np.count_nonzero(moving & (p > ceiling)))
    over = out.above_s / out.moving_s if out.moving_s else 0.0
    out.score = round(min(out.achieved_s / target.target_s, 1.0), 3)
    if over > CEILING_OVER_MAX:
        out.verdict = "missed"
    elif over > CEILING_OVER_OK:
        out.verdict = "partial" if out.score >= PARTIAL_FRAC else "missed"
    else:
        out.verdict = verdict_from_score(out.score)
    if target.floor_w is not None and np_w is not None and np_w < target.floor_w:
        out.too_easy = True
        if out.verdict == "achieved":
            out.verdict = "partial"
    return out


def assess_intent(
    target: IntentTarget,
    watts: FloatArray | None,
    moving: BoolArray,
    *,
    t_s: FloatArray | None = None,
    grade: FloatArray | None = None,
    np_w: float | None = None,
) -> IntentFacts:
    """Assess one ride against ``target`` (1 Hz arrays on the same grid).

    ``np_w`` (the ride's NP) is needed only for a ceiling target's floor.
    """
    out = IntentFacts(moving_s=int(moving.sum()))
    if watts is None or not np.isfinite(watts).any() or target.target_s <= 0:
        return out
    t = np.arange(watts.size) if t_s is None else np.asarray(t_s)
    window = smoothing_window(target)
    p = smooth(watts, window)
    if target.kind == "ceiling":
        return _ceiling(target, p, moving, np_w, out)

    ceiling = target.hi_w * OVERSHOOT_FRAC
    in_band = moving & (p >= target.lo_w) & (p <= ceiling)
    out.achieved_s = int(np.count_nonzero(in_band))
    out.above_s = int(np.count_nonzero(moving & (p > ceiling)))
    score = out.achieved_s / target.target_s
    if target.rep_s and target.reps:
        reps = done = 0
        for parts, gaps in _blocks(p, watts, moving, grade, target.lo_w, window, target.rep_s):
            excused = sum(g.end_s - g.start_s for g in gaps)
            first, last = parts[0][0], parts[-1][1]
            rep_mask = np.ones(last - first, dtype=bool)
            for g in gaps:
                rep_mask[g.start_s - first : g.end_s - first] = False
            seg = np.nan_to_num(watts[first:last], nan=0.0)[rep_mask]
            if seg.size == 0 or float(seg.mean()) < target.lo_w:
                continue
            on_s = last - first - excused
            if on_s >= PARTIAL_REP_FRAC * target.rep_s:
                done += on_s
            n = count_reps(on_s, target.rep_s)
            if n < 1:
                continue
            reps += n
            out.counted.append(Span(int(t[first]), int(t[last - 1]) + 1))
            out.excused += [Span(int(t[g.start_s]), int(t[g.end_s]), g.reason) for g in gaps]
        out.reps_counted = reps
        out.rep_s_done = min(done, target.reps * target.rep_s)
        score = max(score, reps / target.reps, out.rep_s_done / target.target_s)
    out.score = round(min(score, 1.0), 3)
    out.verdict = verdict_from_score(out.score)
    return out


def load_verdict(load_ratio: float | None) -> IntentVerdict:
    """Verdict from the load ratio alone (plans without a power band)."""
    if load_ratio is None:
        return "no_data"
    return verdict_from_score(min(load_ratio, 1.0))
