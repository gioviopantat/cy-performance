"""Data-quality checks on wellness series (resting HR first).

A trained athlete's resting HR moves a few bpm over weeks. An overnight jump of 10+ bpm that
then holds is almost always a change of source (watch out of battery and replaced, another
device or app, daytime instead of overnight HR), not fitness. Reports flag it so a comparison
across the jump is read as reference only.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

#: Readings on each side of a candidate jump (medians, so one odd night does not count).
STEP_SIDE_N = 5
#: A jump this large between the two side medians is treated as a source change.
RHR_STEP_BPM = 10.0


@dataclass(frozen=True)
class StepChange:
    """The largest level shift in a series: the first day of the new level and both levels."""

    day: dt.date
    before: float
    after: float

    @property
    def jump(self) -> float:
        """``after - before``."""
        return self.after - self.before


def step_change(
    series: Sequence[tuple[dt.date, float]],
    *,
    threshold: float = RHR_STEP_BPM,
    side_n: int = STEP_SIDE_N,
) -> StepChange | None:
    """The largest jump between the medians of ``side_n`` readings before and after a day.

    ``series`` is (day, value) with missing days left out; ``None`` when no jump reaches
    ``threshold`` or there are too few readings.
    """
    pts = sorted(series)
    best: tuple[float, float, StepChange] | None = None
    for i in range(side_n, len(pts) - side_n + 1):
        left = [v for _, v in pts[i - side_n : i]]
        right = [v for _, v in pts[i : i + side_n]]
        cand = StepChange(pts[i][0], statistics.median(left), statistics.median(right))
        if abs(cand.jump) < threshold:
            continue
        # Medians decide; the mean breaks ties, so the day of the jump itself wins.
        key = (abs(cand.jump), abs(statistics.fmean(right) - statistics.fmean(left)))
        if best is None or key > best[:2]:
            best = (*key, cand)
    return best[2] if best is not None else None
