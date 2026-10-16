"""Projection of the FTP trajectory to the season target (docs/specs/ride-report.md §3).

Pure. Input: the daily FTP-estimate series of :class:`~cyp.analysis.longitudinal.ftp.FtpStatus`
(icu eFTP, else our rolling 42-day CP fit). Method, deliberately simple and robust:

1. Raw points: the daily series is forward-filled, so only the days where the estimate changed
   are kept (a value repeated for a week is one observation, not seven).
2. Theil–Sen line (median of pairwise slopes) through the raw points of the last
   :data:`TREND_WEEKS` weeks: one odd value moves it little. Its value at ``as_of`` is the
   level the projection starts from (the estimates move with fitness; a fixed FTP setting
   would make the date slide one day per day).
3. Progression must be real, not one step that a window boundary adds or removes: weekly
   medians must rise week over week at least :data:`MIN_WEEKLY_INCREASES` times, the raw points
   of the last half-window must still slope upwards, and Kendall's τ over the weekly medians
   must be ≥ :data:`MIN_KENDALL_TAU`.
4. Projected date = ``as_of`` + (target − level) / slope, only when there are at least
   :data:`MIN_WEEKS` weekly points and :data:`MIN_RAW_POINTS` raw points, the slope is at least
   :data:`MIN_SLOPE_W_PER_WEEK`, the level is not more than :data:`MAX_BELOW_SETTING` below the
   FTP setting (``setting_w``; further below, the estimates come from sub-maximal riding and
   would project too late) and the date is within :data:`MAX_PROJECTION_DAYS`. Otherwise
   ``None``: no date is better than a fake one.
5. A date later than the goal date + :data:`AFTER_TARGET_WEEKS` weeks is not shown either: the
   result is "after the target date" (``after_target``), since a straight line that far out
   says nothing about a date.

A level already at or above the target projects to ``as_of``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise

GOAL_VERSION = "goal_projection_v2"

#: Weeks of estimates the trend is fitted on (the FTP status keeps 8 weeks of daily estimates).
TREND_WEEKS = 8
#: Fewer weekly points than this is not a trend.
MIN_WEEKS = 4
#: Fewer distinct estimate values than this is not a trend (a step is two values).
MIN_RAW_POINTS = 3
#: Week-over-week rises of the weekly median needed: progression across the window, not a step.
MIN_WEEKLY_INCREASES = 3
#: Below this gain per week the line is flat for practical purposes (it would take years).
MIN_SLOPE_W_PER_WEEK = 0.25
#: Kendall's τ over the weekly medians: at 8 points τ = 0.5 is about a one-sided Mann–Kendall
#: p = 0.05.
MIN_KENDALL_TAU = 0.5
#: An estimate level more than 5 % under the FTP setting (CP-model plus power-meter error) means
#: the window holds no maximal efforts (docs/04 "Evidence for downward FTP judgements").
MAX_BELOW_SETTING = 0.05
#: A projected date more than half a season past the goal date is reported as "after the target
#: date" without a date.
AFTER_TARGET_WEEKS = 26
#: Without a goal date, projections further out than two years are not shown.
MAX_PROJECTION_DAYS = 730


@dataclass
class GoalProjection:
    """Result of :func:`project_goal`."""

    as_of: dt.date
    target_w: float
    weekly: list[tuple[dt.date, float]] = field(default_factory=list)
    raw: list[tuple[dt.date, float]] = field(default_factory=list)
    slope_w_per_week: float | None = None
    #: Theil–Sen slope of the last half-window's raw points.
    recent_slope_w_per_week: float | None = None
    weekly_increases: int | None = None
    kendall_tau: float | None = None
    #: The Theil–Sen line at ``as_of``: the level the projection starts from.
    level_w: float | None = None
    projected_date: dt.date | None = None
    #: The trend reaches the target only after the goal date + :data:`AFTER_TARGET_WEEKS`.
    after_target: bool = False
    #: Why there is no date: ``too_few_weeks`` | ``too_few_points`` | ``below_setting`` |
    #: ``flat_or_falling`` | ``no_progression`` | ``stalled`` | ``inconsistent`` |
    #: ``after_target`` | ``too_far`` | ``None``.
    reason: str | None = None


def raw_points(
    estimates: Mapping[dt.date, float], as_of: dt.date, weeks: int = TREND_WEEKS
) -> list[tuple[dt.date, float]]:
    """Days in the window where the estimate changed (plus the first day), oldest first."""
    start = as_of - dt.timedelta(days=7 * weeks - 1)
    out: list[tuple[dt.date, float]] = []
    for d in sorted(estimates):
        if not start <= d <= as_of:
            continue
        v = float(estimates[d])
        if not out or v != out[-1][1]:
            out.append((d, v))
    return out


def weekly_medians(
    estimates: Mapping[dt.date, float], as_of: dt.date, weeks: int = TREND_WEEKS
) -> list[tuple[dt.date, float]]:
    """Median estimate per 7-day window ending at ``as_of`` (oldest first).

    Each item is ``(last day of the window, median)``; windows without estimates are skipped.
    """
    out: list[tuple[dt.date, float]] = []
    for k in range(weeks - 1, -1, -1):
        end = as_of - dt.timedelta(days=7 * k)
        start = end - dt.timedelta(days=6)
        vals = [v for d, v in estimates.items() if start <= d <= end]
        if vals:
            out.append((end, float(statistics.median(vals))))
    return out


def theil_sen(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """(slope, intercept) of the Theil–Sen line; ``None`` with fewer than two distinct x."""
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1 :]
        if x2 != x1
    ]
    if not slopes:
        return None
    slope = statistics.median(slopes)
    intercept = statistics.median([y - slope * x for x, y in points])
    return slope, intercept


def kendall_tau(ys: list[float]) -> float | None:
    """(concordant − discordant) / pairs over ``ys`` in time order (ties count as neither)."""
    n = len(ys)
    if n < 2:
        return None
    s = sum((ys[j] > ys[i]) - (ys[j] < ys[i]) for i in range(n) for j in range(i + 1, n))
    return s / (n * (n - 1) / 2)


def _weeks(points: list[tuple[dt.date, float]], as_of: dt.date) -> list[tuple[float, float]]:
    return [((d - as_of).days / 7.0, v) for d, v in points]


def project_goal(
    estimates: Mapping[dt.date, float],
    *,
    as_of: dt.date,
    target_w: float,
    target_date: dt.date | None = None,
    setting_w: float | None = None,
) -> GoalProjection:
    """Project the estimate trend to ``target_w`` (see module docstring)."""
    proj = GoalProjection(as_of=as_of, target_w=target_w)
    proj.weekly = weekly_medians(estimates, as_of)
    proj.raw = raw_points(estimates, as_of)
    if len(proj.weekly) < MIN_WEEKS:
        proj.reason = "too_few_weeks"
        return proj
    fit = theil_sen(_weeks(proj.raw, as_of))
    if len(proj.raw) < MIN_RAW_POINTS or fit is None:
        proj.reason = "too_few_points"
        if proj.raw:
            proj.level_w = round(proj.raw[-1][1], 1)
        return proj
    slope, intercept = fit
    ys = [v for _, v in proj.weekly]
    proj.slope_w_per_week = round(slope, 3)
    proj.level_w = round(intercept, 1)  # x = 0 is as_of
    proj.weekly_increases = sum(b > a for a, b in pairwise(ys))
    proj.kendall_tau = round(kendall_tau(ys) or 0.0, 3)
    half = as_of - dt.timedelta(days=7 * TREND_WEEKS // 2)
    recent = theil_sen(_weeks([p for p in proj.raw if p[0] > half], as_of))
    proj.recent_slope_w_per_week = round(recent[0], 3) if recent else 0.0

    if proj.level_w >= target_w:
        proj.projected_date = as_of
        return proj
    if setting_w and proj.level_w < (1 - MAX_BELOW_SETTING) * setting_w:
        proj.reason = "below_setting"
    elif slope < MIN_SLOPE_W_PER_WEEK:
        proj.reason = "flat_or_falling"
    elif proj.weekly_increases < MIN_WEEKLY_INCREASES:
        proj.reason = "no_progression"
    elif proj.recent_slope_w_per_week <= 0:
        proj.reason = "stalled"
    elif proj.kendall_tau < MIN_KENDALL_TAU:
        proj.reason = "inconsistent"
    if proj.reason is not None:
        return proj
    date = as_of + dt.timedelta(days=round((target_w - proj.level_w) / slope * 7))
    if target_date is not None and date > target_date + dt.timedelta(weeks=AFTER_TARGET_WEEKS):
        proj.after_target = True
        proj.reason = "after_target"
    elif (date - as_of).days > MAX_PROJECTION_DAYS:
        proj.reason = "too_far"
    else:
        proj.projected_date = date
    return proj
