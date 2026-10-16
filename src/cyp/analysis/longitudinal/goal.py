"""Projection of the FTP trajectory to the season target (docs/specs/ride-report.md §3).

Pure. Input: the daily FTP-estimate series of :class:`~cyp.analysis.longitudinal.ftp.FtpStatus`
(icu eFTP, else our rolling 42-day CP fit). Method, deliberately simple and robust:

1. Weekly medians of the estimates over the last :data:`TREND_WEEKS` weeks (the daily series
   is forward-filled, so medians per week avoid counting one value seven times).
2. Theil–Sen slope (median of pairwise slopes) over those weekly points: one odd week moves
   it little.
3. Kendall's τ over the same points: the trend must be consistent (most week-to-week
   comparisons go up), not one step that a window boundary can add or remove.
4. Level today = the Theil–Sen line at ``as_of`` (the estimates move with fitness; a fixed
   FTP setting would make the date slide one day per day).
5. Projected date = ``as_of`` + (target − level) / slope, only when there are at least
   :data:`MIN_WEEKS` weekly points, τ ≥ :data:`MIN_KENDALL_TAU`, the slope is at least
   :data:`MIN_SLOPE_W_PER_WEEK`, the level is not more than :data:`MAX_BELOW_SETTING` below the
   FTP setting (``setting_w``; further below, the estimates come from sub-maximal riding and
   would project too late) and the date is within :data:`MAX_PROJECTION_DAYS`. Otherwise
   ``None``: no date is better than a fake one.

A level already at or above the target projects to ``as_of``.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field

GOAL_VERSION = "goal_projection_v1"

#: Weeks of estimates the trend is fitted on (the FTP status keeps 8 weeks of daily estimates).
TREND_WEEKS = 8
#: Fewer weekly points than this is not a trend.
MIN_WEEKS = 4
#: Below this gain per week the line is flat for practical purposes (it would take years).
MIN_SLOPE_W_PER_WEEK = 0.25
#: Kendall's τ needed for a trend: at 8 weekly points τ = 0.5 is about a one-sided Mann–Kendall
#: p = 0.05, so a single step up (one window boundary) does not count as progress.
MIN_KENDALL_TAU = 0.5
#: An estimate level more than 5 % under the FTP setting (CP-model plus power-meter error) means
#: the window holds no maximal efforts (docs/04 "Evidence for downward FTP judgements").
MAX_BELOW_SETTING = 0.05
#: Projections further out than two years are not shown.
MAX_PROJECTION_DAYS = 730


@dataclass
class GoalProjection:
    """Result of :func:`project_goal`."""

    as_of: dt.date
    target_w: float
    weekly: list[tuple[dt.date, float]] = field(default_factory=list)
    slope_w_per_week: float | None = None
    kendall_tau: float | None = None
    #: The Theil–Sen line at ``as_of``: the level the projection starts from.
    level_w: float | None = None
    projected_date: dt.date | None = None
    #: Why there is no date: ``too_few_weeks`` | ``below_setting`` | ``flat_or_falling`` |
    #: ``inconsistent`` | ``too_far`` | ``None``.
    reason: str | None = None


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


def project_goal(
    estimates: Mapping[dt.date, float],
    *,
    as_of: dt.date,
    target_w: float,
    setting_w: float | None = None,
) -> GoalProjection:
    """Project the estimate trend to ``target_w`` (see module docstring)."""
    proj = GoalProjection(as_of=as_of, target_w=target_w)
    proj.weekly = weekly_medians(estimates, as_of)
    if len(proj.weekly) < MIN_WEEKS:
        proj.reason = "too_few_weeks"
        return proj
    pts = [((d - as_of).days / 7.0, v) for d, v in proj.weekly]
    fit = theil_sen(pts)
    if fit is None:  # pragma: no cover - weekly points always have distinct x
        proj.reason = "too_few_weeks"
        return proj
    slope, intercept = fit
    proj.slope_w_per_week = round(slope, 3)
    proj.kendall_tau = round(kendall_tau([v for _, v in proj.weekly]) or 0.0, 3)
    proj.level_w = round(intercept, 1)  # x = 0 is as_of
    if proj.level_w >= target_w:
        proj.projected_date = as_of
        return proj
    if setting_w and proj.level_w < (1 - MAX_BELOW_SETTING) * setting_w:
        proj.reason = "below_setting"
        return proj
    if slope < MIN_SLOPE_W_PER_WEEK:
        proj.reason = "flat_or_falling"
        return proj
    if proj.kendall_tau < MIN_KENDALL_TAU:
        proj.reason = "inconsistent"
        return proj
    days = round((target_w - proj.level_w) / slope * 7)
    if days > MAX_PROJECTION_DAYS:
        proj.reason = "too_far"
        return proj
    proj.projected_date = as_of + dt.timedelta(days=days)
    return proj
