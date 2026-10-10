"""Suggest a local climb for long outdoor work steps (``location.climbs`` in athlete.yaml).

Steady power is easiest to hold on a steady climb, so outdoor sessions whose longest hard step
lasts ≥ :data:`MIN_STEP_MIN` minutes get one footer line naming a climb that fits it:

1. Candidates are climbs whose ``good_for`` contains the session intent (empty = any).
2. The shortest candidate whose ``minutes_min`` covers the step wins (the step fits in one
   ascent with the least extra climbing).
3. If none is long enough, the longest candidate is named with a hint to turn at the top or
   finish on the flat.

Pure: no I/O; the planner job passes the configured climbs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cyp.planning.templates import Repeat, ResolvedWorkout, Step

#: Shorter hard steps are easy to place anywhere.
MIN_STEP_MIN = 8.0
#: Steps at or above this % FTP (or % LTHR) count as "hard" work (tempo and up).
HARD_PCT = 76.0


@dataclass(frozen=True)
class Climb:
    """The subset of ``ClimbConfig`` this module needs."""

    name_zh: str
    minutes_min: float
    minutes_max: float
    grade_pct: float
    good_for: tuple[str, ...] = ()


def _steps(w: ResolvedWorkout) -> list[Step]:
    out: list[Step] = []
    for item in w.items:
        out.extend(item.steps if isinstance(item, Repeat) else (item,))
    return out


def longest_hard_step_min(w: ResolvedWorkout) -> float:
    """Longest work step (``kind == "work"`` or target ≥ :data:`HARD_PCT`), in minutes."""
    best = 0
    for st in _steps(w):
        hard = st.kind == "work" or (st.lo is not None and st.lo >= HARD_PCT)
        if hard and st.duration_s > best:
            best = st.duration_s
    return best / 60


def suggest(climbs: Sequence[Climb], step_min: float, intent: str) -> tuple[Climb, bool] | None:
    """``(climb, fits_in_one_ascent)`` or ``None`` when no climb is configured for ``intent``."""
    cands = [c for c in climbs if not c.good_for or intent in c.good_for]
    if not cands:
        return None
    fitting = [c for c in cands if c.minutes_min >= step_min]
    if fitting:
        return min(fitting, key=lambda c: c.minutes_min), True
    return max(cands, key=lambda c: c.minutes_max), False


def footer_line(climbs: Sequence[Climb], w: ResolvedWorkout, intent: str) -> str | None:
    """``建議路段：…`` for an outdoor workout, or ``None`` (indoor / short steps / no climb)."""
    if not w.outdoor or not climbs:
        return None
    step = longest_hard_step_min(w)
    if step < MIN_STEP_MIN:
        return None
    hit = suggest(climbs, step, intent)
    if hit is None:
        return None
    c, fits = hit
    line = (
        f"建議路段：{c.name_zh}（約 {c.minutes_min:.0f}–{c.minutes_max:.0f} 分，{c.grade_pct:g} %）"
    )
    if not fits:
        line += f"；每段 {step:.0f} 分鐘比坡長：可在坡頂掉頭續做／改在平路完成"
    return line
