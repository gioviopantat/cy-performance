"""v1 intent check: did a ride deliver the planned stimulus (docs/specs/ride-report.md §2)?

Pure. The planner (layer 5) is not imported: the caller resolves the planned workout and passes
an :class:`IntentTarget` in watts. Outdoor interruptions are *excused*, not failures.

Two kinds of target:

- ``band`` (tempo, sweet spot, threshold, VO2, …): the work steps' power band. Achieved
  minutes are moving seconds whose smoothed power lies in ``[lo, hi × OVERSHOOT_FRAC]``.
  A *rep* is a block of runs of smoothed power ≥ ``lo``. Two runs belong to one block when the
  gap between them is a *dip* (shorter than the smoothing window, or ≤ :data:`EXCUSE_MAX_S` at
  ≥ :data:`DIP_FLOOR_FRAC` × ``lo`` on average: still pedalling hard, e.g. a steeper ramp or a
  shift on a climb) or an *excused* interruption (≤ :data:`EXCUSE_MAX_S`, mostly stopped,
  coasting or descending; listed, and not counted as rep time). A block counts when its mean
  power over the rep time is ≥ ``lo`` and the rep time holds :data:`REP_COUNT_FRAC` of the
  planned rep length (a 24-min block counts as two 12-min reps).
- ``ceiling`` (recovery, endurance): the point is to stay easy for the planned time. Achieved
  minutes are moving seconds at or below ``hi × OVERSHOOT_FRAC`` (coasting counts: descents
  are part of an easy road ride); time above the ceiling is what fails it.

Score: achieved / planned minutes in the band (the spec's v1 measure); for rep workouts the
larger of that and reps counted / planned, because a counted rep already has its mean power in
the band and second-by-second dips on a climb would otherwise hide a delivered stimulus. Verdict
thresholds are the named constants below. Load ratio (actual / planned load) is
reported alongside; it decides the verdict only when the plan has no power band (e.g. an
intervals.icu workout without our template).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from cyp.analysis.ride.frames import BoolArray, FloatArray

INTENT_VERSION = "ride_intent_v1"

IntentKind = Literal["band", "ceiling"]
IntentVerdict = Literal["achieved", "partial", "missed", "no_data"]

#: Smoothing window (s) for power, as for NP: one-second spikes and dips are not the stimulus.
SMOOTH_S = 30
#: Smoothing never exceeds a quarter of a rep, so 30/15 or 40/20 reps stay visible.
SMOOTH_REP_FRACTION = 0.25
#: Up to 10 % over the band top still delivers the planned stimulus (and a little more).
OVERSHOOT_FRAC = 1.10
#: A rep counts once it holds 80 % of its planned length (Coggan-style compliance on work steps).
REP_COUNT_FRAC = 0.80
#: A gap of up to this long at ≥ 80 % of the band floor is a dip inside a rep, not its end
#: (outdoor power on a climb oscillates around the floor; 80 % is still tempo for sweet spot).
DIP_FLOOR_FRAC = 0.80
#: The longest interruption that can be excused inside one rep (a long red light).
EXCUSE_MAX_S = 120
#: An interruption is excused when at least half of it was stopped, coasting or descending.
EXCUSE_SHARE = 0.5
#: Grade (%) at or below which a second counts as descending.
DESCENT_GRADE_PCT = -2.0
#: Score (achieved / planned) from which the stimulus counts as reached.
ACHIEVED_FRAC = 0.90
#: Score from which it counts as partly reached; below is missed.
PARTIAL_FRAC = 0.50
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
    counted: list[Span] = field(default_factory=list)
    excused: list[Span] = field(default_factory=list)
    score: float | None = None
    verdict: IntentVerdict = "no_data"


def smooth(watts: FloatArray, window: int) -> FloatArray:
    """Centred moving average with NaN as 0 W (paused seconds pull the average down)."""
    x = np.nan_to_num(np.asarray(watts, dtype=np.float64), nan=0.0)
    w = max(1, min(int(window), x.size))
    kernel = np.ones(w) / w
    return np.asarray(np.convolve(x, kernel, mode="same"), dtype=np.float64)


def _runs(mask: BoolArray) -> list[tuple[int, int]]:
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return [(int(s), int(e)) for s, e in zip(edges[::2], edges[1::2], strict=True)]


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
) -> list[tuple[list[tuple[int, int]], list[Span]]]:
    """Runs of ``p ≥ lo_w`` merged across dips and excused gaps (indices, not seconds).

    Returns ``(runs, excused gaps)`` per block; gap spans hold grid indices.
    """
    blocks: list[tuple[list[tuple[int, int]], list[Span]]] = []
    for r in _runs(p >= lo_w):
        if blocks:
            prev_end = blocks[-1][0][-1][1]
            gap = r[0] - prev_end
            reason = _gap_reason(prev_end, r[0], moving, watts, grade)
            if reason is not None:
                blocks[-1][0].append(r)
                blocks[-1][1].append(Span(prev_end, r[0], reason))
                continue
            dip_mean = float(np.nan_to_num(watts[prev_end : r[0]], nan=0.0).mean())
            if gap <= window or (gap <= EXCUSE_MAX_S and dip_mean >= DIP_FLOOR_FRAC * lo_w):
                blocks[-1][0].append(r)
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


def assess_intent(
    target: IntentTarget,
    watts: FloatArray | None,
    moving: BoolArray,
    *,
    t_s: FloatArray | None = None,
    grade: FloatArray | None = None,
) -> IntentFacts:
    """Assess one ride against ``target`` (1 Hz arrays on the same grid)."""
    out = IntentFacts(moving_s=int(moving.sum()))
    if watts is None or not np.isfinite(watts).any() or target.target_s <= 0:
        return out
    t = np.arange(watts.size) if t_s is None else np.asarray(t_s)
    window = SMOOTH_S
    if target.rep_s:
        window = max(3, min(SMOOTH_S, int(target.rep_s * SMOOTH_REP_FRACTION)))
    p = smooth(watts, window)
    ceiling = target.hi_w * OVERSHOOT_FRAC

    if target.kind == "ceiling":
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
        return out

    in_band = moving & (p >= target.lo_w) & (p <= ceiling)
    out.achieved_s = int(np.count_nonzero(in_band))
    out.above_s = int(np.count_nonzero(moving & (p > ceiling)))
    score = out.achieved_s / target.target_s
    if target.rep_s and target.reps:
        reps = 0
        for parts, gaps in _blocks(p, watts, moving, grade, target.lo_w, window):
            excused = sum(g.end_s - g.start_s for g in gaps)
            first, last = parts[0][0], parts[-1][1]
            rep_mask = np.ones(last - first, dtype=bool)
            for g in gaps:
                rep_mask[g.start_s - first : g.end_s - first] = False
            seg = np.nan_to_num(watts[first:last], nan=0.0)[rep_mask]
            if seg.size == 0 or float(seg.mean()) < target.lo_w:
                continue
            n = count_reps(last - first - excused, target.rep_s)
            if n < 1:
                continue
            reps += n
            out.counted.append(Span(int(t[first]), int(t[last - 1]) + 1))
            out.excused += [Span(int(t[g.start_s]), int(t[g.end_s]), g.reason) for g in gaps]
        out.reps_counted = reps
        score = max(score, reps / target.reps)
    out.score = round(min(score, 1.0), 3)
    out.verdict = verdict_from_score(out.score)
    return out


def load_verdict(load_ratio: float | None) -> IntentVerdict:
    """Verdict from the load ratio alone (plans without a power band)."""
    if load_ratio is None:
        return "no_data"
    return verdict_from_score(min(load_ratio, 1.0))
