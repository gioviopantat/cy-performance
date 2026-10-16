"""Charts for the weekly / monthly digest PDFs (docs/specs/digest-reports.md): matplotlib -> SVG.

Same palette, fonts and chrome as the per-ride charts (:mod:`cyp.reports.charts`). Each chart
carries our verdict (plan vs done, intensity vs the phase target, HRV vs its baseline, CTL vs
the season path, limiter ranks); they read only :mod:`cyp.reports.digest_model` fields and
compute nothing beyond plotting. Two measures of different scale go in stacked panels that
share the x axis, never on a second y axis. Intensity bands use a one-hue ordinal ramp.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, MaxNLocator

from cyp.reports.charts import (
    AXIS,
    GRID,
    INK,
    INK_2,
    MUTED,
    SERIES_1,
    SERIES_2,
    SURFACE,
    WIDTH_IN,
    _chrome,
    _dn,
    _no_data,
    _rc,
    _svg,
    _t,
    _unit,
    climb_history,
    data_uri,
    ftp_trajectory,
)
from cyp.reports.digest_model import (
    Band,
    Consistency,
    Distribution,
    GoalProgress,
    LimiterShift,
    Load,
    MonthlyDigest,
    PowerCurve,
    Recovery,
    WeeklyDigest,
)
from cyp.reports.ride_model import Goal

#: Ordinal blue ramp (dataviz palette steps 250 / 400 / 600, validated with ``--ordinal``).
BAND_COLOURS: dict[Band, str] = {"low": "#86b6ef", "mid": "#3987e5", "high": "#184f95"}
BANDS: tuple[Band, ...] = ("low", "mid", "high")
PLANNED = "#c3c2b7"  # planned load: recessive grey next to the measured blue
GAP = 1.4  # pt; surface gap between adjacent fills


@contextmanager
def _panels(height_in: float, ratios: Sequence[float]) -> Iterator[tuple[Figure, list[Axes]]]:
    """Stacked panels sharing the x axis (one measure per panel, never a twin y axis)."""
    with matplotlib.rc_context(_rc()):
        # Constrained layout reserves room for the legend below the tick labels (_legend).
        fig = Figure(figsize=(WIDTH_IN, height_in), facecolor=SURFACE, layout="constrained")
        fig.get_layout_engine().set(hspace=0.08)  # type: ignore[union-attr, call-arg]
        axes = fig.subplots(len(ratios), 1, sharex=True, gridspec_kw={"height_ratios": ratios})
        axs: list[Axes] = list(axes) if len(ratios) > 1 else [axes]
        for ax in axs:
            _chrome(ax)
        yield fig, axs


def _day_axis(ax: Axes, days: Sequence[dt.date]) -> None:
    """One tick per day for a week (weekday and ``mm/dd``), else weekly date ticks."""
    if len(days) <= 8:
        names = _t("weekdays")
        ax.xaxis.set_major_locator(FixedLocator([_dn(d) for d in days]))
        ax.set_xticklabels([_t("day_tick", wd=names[d.weekday()], date=f"{d:%m/%d}") for d in days])
    else:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO))  # type: ignore[no-untyped-call]
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))  # type: ignore[no-untyped-call]
    if days:
        ax.set_xlim(_dn(min(days)) - 0.6, _dn(max(days)) + 0.6)


def _legend(fig: Figure, handles: list[Any], ncol: int | None = None) -> None:
    fig.legend(
        handles=handles,
        loc="outside lower center",
        ncol=ncol or min(len(handles), 4),
    )


def _end_label(ax: Axes, day: dt.date, value: float, text: str, *, dy: int = 5) -> None:
    ax.annotate(
        text,
        (_dn(day), value),
        xytext=(4, dy),
        textcoords="offset points",
        ha="left",
        va="bottom",
        color=INK,
        fontsize=7.5,
    )


def _ctl_lines(ax: Axes, load: Load, handles: list[Any]) -> None:
    if load.ctl_plan:
        ax.plot(
            [_dn(d) for d, _ in load.ctl_plan],
            [v for _, v in load.ctl_plan],
            color=SERIES_2,
            lw=1.2,
            ls=(0, (1.5, 2)),
        )
        handles.append(
            Line2D([], [], color=SERIES_2, lw=1.2, ls=(0, (1.5, 2)), label=_t("ctl_plan"))
        )
    if load.ctl_series:
        days = [d for d, _ in load.ctl_series]
        values = [v for _, v in load.ctl_series]
        ax.plot([_dn(d) for d in days], values, color=SERIES_1, lw=1.6)
        ax.plot([_dn(days[-1])], [values[-1]], "o", color=SERIES_1, ms=5, mec=SURFACE, mew=1)
        _end_label(ax, days[-1], values[-1], f"{values[-1]:.0f}")
        if len(days) > 1:
            ax.plot([_dn(days[0])], [values[0]], "o", color=SERIES_1, ms=4, mec=SURFACE, mew=1)
        handles.append(Line2D([], [], color=SERIES_1, lw=1.6, label=_t("ctl")))
    _unit(ax, "CTL")


def load_week(load: Load) -> bytes:
    """Weekly section 3: planned vs actual TSS per day; CTL across the week below it."""
    days = [d for d, _, _ in load.daily] or [d for d, _ in load.ctl_series]
    with _panels(2.4, (1.6, 1.0)) as (fig, (top, bottom)):
        x = [_dn(d) for d, _, _ in load.daily]
        width = 0.36
        top.bar(
            [v - width / 2 for v in x],
            [p for _, p, _ in load.daily],
            width=width,
            color=PLANNED,
            edgecolor=SURFACE,
            linewidth=GAP,
        )
        top.bar(
            [v + width / 2 for v in x],
            [a for _, _, a in load.daily],
            width=width,
            color=SERIES_1,
            edgecolor=SURFACE,
            linewidth=GAP,
        )
        top.set_ylim(bottom=0)
        top.yaxis.set_major_locator(MaxNLocator(4))
        _unit(top, "TSS")
        handles: list[Any] = [
            Patch(color=PLANNED, label=_t("tss_planned")),
            Patch(color=SERIES_1, label=_t("tss_actual")),
        ]
        _ctl_lines(bottom, load, handles)
        bottom.yaxis.set_major_locator(MaxNLocator(3))
        bottom.margins(y=0.35)
        _day_axis(bottom, days)
        _legend(fig, handles)
        return _svg(fig)


def ctl_path(load: Load, *, height_in: float = 1.8) -> bytes:
    """Monthly section 3: CTL over the month against the season plan's CTL path.

    Also the CTL drift of a break period (a lower chart, so the short version fits one page).
    """
    with _panels(height_in, (1.0,)) as (fig, (ax,)):
        handles: list[Any] = []
        _ctl_lines(ax, load, handles)
        days = [d for d, _ in [*load.ctl_series, *load.ctl_plan]]
        if not days:
            _no_data(ax, _t("ctl"))
        ax.margins(y=0.25)
        _day_axis(ax, sorted(set(days)))
        if handles:
            _legend(fig, handles)
        return _svg(fig)


def _segment_label(ax: Axes, left: float, width: float, y: float, band: Band) -> None:
    if width < 0.09:
        return  # too narrow to hold a label with padding; the table carries the value
    ax.text(
        left + width / 2,
        y,
        f"{width * 100:.0f}%",
        ha="center",
        va="center",
        fontsize=7.5,
        color=INK if band == "low" else SURFACE,
    )


def distribution_rows(
    rows: Sequence[tuple[str, dict[Band, float], dict[Band, float] | None]],
) -> bytes:
    """Weekly section 4 / monthly section 6: 100 % bars of low / mid / high.

    Each row is ``(label, fractions, phase target)``; the target is drawn as two boundary
    ticks (low|mid, mid|high) on the row so the gap to it is read off directly.
    """
    with _panels(0.45 + 0.34 * len(rows), (1.0,)) as (fig, (ax,)):
        ax.grid(False)
        ax.spines["bottom"].set_visible(False)
        has_target = False
        for i, (_, frac, target) in enumerate(rows):
            y = len(rows) - 1 - i
            left = 0.0
            for band in BANDS:
                width = frac.get(band, 0.0)
                if width > 0:
                    ax.barh(
                        y,
                        width,
                        left=left,
                        height=0.62,
                        color=BAND_COLOURS[band],
                        edgecolor=SURFACE,
                        linewidth=GAP,
                    )
                    _segment_label(ax, left, width, y, band)
                left += width
            if target is not None:
                has_target = True
                for edge in (
                    target.get("low", 0.0),
                    target.get("low", 0.0) + target.get("mid", 0.0),
                ):
                    ax.plot(
                        [edge, edge], [y - 0.42, y + 0.42], color=INK, lw=1.4, solid_capstyle="butt"
                    )
        ax.set_yticks(range(len(rows)))
        ax.set_yticklabels([label for label, _, _ in reversed(rows)])
        ax.set_xlim(0, 1)
        ax.set_ylim(-0.6, len(rows) - 0.4)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_xticklabels(["0", "25", "50", "75", "100 %"])
        handles: list[Any] = [Patch(color=BAND_COLOURS[b], label=_t(b)) for b in BANDS]
        if has_target:
            handles.append(Line2D([], [], color=INK, lw=1.4, label=_t("target_split")))
        _legend(fig, handles)
        return _svg(fig)


def weekly_distribution(dist: Distribution) -> bytes:
    """Weekly section 4: this week's sessions vs the phase target, time in zone as reference."""
    rows: list[tuple[str, dict[Band, float], dict[Band, float] | None]] = [
        (_t("row_sessions"), dist.session_frac, dist.target_frac)
    ]
    if dist.target_frac is not None:
        rows.append((_t("row_target"), dist.target_frac, None))
    if dist.time_frac is not None:
        rows.append((_t("row_time"), dist.time_frac, None))
    return distribution_rows(rows)


def monthly_distribution(weeks: Sequence[tuple[dt.date, Distribution]]) -> bytes:
    """Monthly section 6: sessions per week, each against its own phase target."""
    return distribution_rows(
        [(_t("week_of", date=f"{d:%m/%d}"), w.session_frac, w.target_frac) for d, w in weeks]
    )


def recovery_days(recovery: Recovery) -> bytes:
    """Weekly section 6 / monthly section 8: HRV (7-day z vs baseline band) above resting HR."""
    hrv = [(d.day, d.hrv_z7) for d in recovery.days if d.hrv_z7 is not None]
    rhr = [(d.day, d.rhr) for d in recovery.days if d.rhr is not None]
    days = [d.day for d in recovery.days]
    ratios = (1.3, 1.0) if hrv and rhr else (1.0,)
    with _panels(2.5 if len(ratios) == 2 else 1.5, ratios) as (fig, axs):
        handles: list[Any] = []
        ax = axs[0]
        if hrv:
            ax.axhspan(-1, 1, color=GRID, alpha=0.7, lw=0)
            ax.axhline(0, color=AXIS, lw=0.8)
            ax.plot(
                [_dn(d) for d, _ in hrv],
                [v for _, v in hrv],
                color=SERIES_1,
                lw=1.6,
                marker="o",
                ms=4,
                mec=SURFACE,
                mew=1,
            )
            low = min(-1.6, *(v for _, v in hrv)) - 0.2
            high = max(1.6, *(v for _, v in hrv)) + 0.2
            ax.set_ylim(low, high)
            _unit(ax, "z")
            handles += [
                Line2D([], [], color=SERIES_1, lw=1.6, marker="o", label=_t("hrv_z7")),
                Patch(color=GRID, label=_t("hrv_band")),
            ]
            ax = axs[-1]
        if rhr:
            ax.plot(
                [_dn(d) for d, _ in rhr],
                [v for _, v in rhr],
                color=SERIES_2,
                lw=1.6,
                marker="o",
                ms=4,
                mec=SURFACE,
                mew=1,
            )
            ax.margins(y=0.3)
            ax.yaxis.set_major_locator(MaxNLocator(3, integer=True))
            _unit(ax, "bpm")
            handles.append(Line2D([], [], color=SERIES_2, lw=1.6, marker="o", label=_t("rhr")))
        if not hrv and not rhr:
            ax.set_yticks([])
            _no_data(ax, _t("no_recovery"))
        _day_axis(axs[-1], days)
        if handles:
            _legend(fig, handles)
        return _svg(fig)


def as_goal(goal: GoalProgress) -> Goal:
    """The per-ride chart's input for a digest's goal (same fields, the period's evidence)."""
    return Goal(
        target_w=goal.target_w,
        target_date=goal.target_date,
        current_ftp_w=goal.current_ftp_w,
        current_source="digest",
        trend_level_w=goal.trend_level_w,
        this_ride_evidence_w=goal.evidence_w,
        projected_date=goal.projected_date,
        history=goal.history,
    )


def goal_trajectory(goal: GoalProgress, today: dt.date) -> bytes:
    """Weekly section 5 / monthly section 5: the per-ride FTP trajectory chart, reused."""
    return ftp_trajectory(as_goal(goal), today, evidence_key="period_evidence")


def consistency_weeks(c: Consistency) -> bytes:
    """Monthly section 2: sessions planned / done / achieved, per week."""
    with _panels(1.6, (1.0,)) as (fig, (ax,)):
        width = 0.26
        series = (
            ([p for _, p, _, _ in c.by_week], PLANNED, "planned"),
            ([d for _, _, d, _ in c.by_week], BAND_COLOURS["low"], "done"),
            ([a for _, _, _, a in c.by_week], BAND_COLOURS["high"], "achieved"),
        )
        for i, (counts, colour, _) in enumerate(series):
            ax.bar(
                [k + (i - 1) * width for k in range(len(c.by_week))],
                counts,
                width=width,
                color=colour,
                edgecolor=SURFACE,
                linewidth=GAP,
            )
        ax.set_xticks(range(len(c.by_week)))
        ax.set_xticklabels([_t("week_of", date=f"{w[0]:%m/%d}") for w in c.by_week])
        ax.set_xlim(-0.6, max(len(c.by_week), 1) - 0.4)
        ax.yaxis.set_major_locator(MaxNLocator(4, integer=True))
        ax.set_ylim(bottom=0)
        _unit(ax, _t("sessions_unit"))
        _legend(fig, [Patch(color=colour, label=_t(key)) for _, colour, key in series])
        return _svg(fig)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600 or seconds % 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _curve(ax: Axes, xs: Sequence[int], ys: Sequence[float | None], **kw: Any) -> bool:
    pts = [(x, y) for x, y in zip(xs, ys, strict=False) if y is not None]
    if pts:
        ax.plot([x for x, _ in pts], [y for _, y in pts], **kw)
    return bool(pts)


def power_curve(pc: PowerCurve) -> bytes:
    """Monthly section 4: best power by duration (log x), this vs last month vs season best."""
    with _panels(2.0, (1.0,)) as (fig, (ax,)):
        handles: list[Any] = []
        lines: tuple[tuple[list[float | None], dict[str, Any], str], ...] = (
            (pc.season_best_w, dict(color=INK_2, lw=1.0), "season_best"),
            (pc.last_month_w, dict(color=SERIES_2, lw=1.4), "last_month"),
            (
                pc.this_month_w,
                dict(color=SERIES_1, lw=2.0, marker="o", ms=4, mec=SURFACE, mew=1),
                "this_month",
            ),
        )
        for ys, style, key in lines:
            if _curve(ax, pc.durations_s, ys, **style):
                handles.insert(0, Line2D([], [], label=_t(key), **style))
        if not handles or not pc.durations_s:
            _no_data(ax, _t("no_power_curve"))
        else:
            ax.set_xscale("log")
            ax.xaxis.set_major_locator(FixedLocator(pc.durations_s))
            ax.xaxis.set_minor_locator(FixedLocator([]))
            ax.set_xticklabels([_duration(s) for s in pc.durations_s])
            ax.grid(axis="x", color=GRID, linewidth=0.6)
            ax.margins(x=0.03, y=0.12)
            _legend(fig, handles, ncol=3)
        _unit(ax, "W")
        return _svg(fig)


def limiter_slope(shift: LimiterShift) -> bytes:
    """Monthly section 7: each limiter's rank a month ago -> now (1 = the most limiting)."""
    lines = [ln for ln in shift.lines if ln.rank_now is not None or ln.rank_before is not None]
    worst = max([1, *(r for ln in lines for r in (ln.rank_now, ln.rank_before) if r is not None)])
    with _panels(0.5 + 0.28 * worst, (1.0,)) as (fig, (ax,)):
        ax.grid(False)
        ax.spines["bottom"].set_visible(False)
        for ln in lines:
            if ln.rank_before is not None and ln.rank_now is not None:
                ax.plot(
                    [0, 1],
                    [ln.rank_before, ln.rank_now],
                    color=SERIES_1,
                    lw=1.6,
                    marker="o",
                    ms=5,
                    mec=SURFACE,
                    mew=1,
                )
            elif ln.rank_now is not None:
                ax.plot([1], [ln.rank_now], "o", mfc=SURFACE, mec=SERIES_1, ms=6, mew=1.4)
            elif ln.rank_before is not None:
                ax.plot([0], [ln.rank_before], "o", color=MUTED, ms=5, mec=SURFACE, mew=1)
            if ln.rank_before is not None:
                before = (
                    f"{ln.rank_before}"
                    if ln.rank_now is not None
                    else f"{ln.name_zh}（{_t('dropped_limiter')}）  {ln.rank_before}"
                )
                ax.annotate(
                    before,
                    (0, ln.rank_before),
                    xytext=(-8, 0),
                    textcoords="offset points",
                    ha="right",
                    va="center",
                    color=INK_2,
                )
            if ln.rank_now is not None:
                suffix = "" if ln.rank_before is not None else f"（{_t('new_limiter')}）"
                ax.annotate(
                    f"{ln.rank_now}  {ln.name_zh}{suffix}",
                    (1, ln.rank_now),
                    xytext=(8, 0),
                    textcoords="offset points",
                    ha="left",
                    va="center",
                    color=INK,
                )
        ax.set_xlim(-1.4, 2.4)
        ax.set_ylim(worst + 0.6, 0.4)
        ax.set_yticks([])
        ax.set_xticks([0, 1])
        ax.set_xticklabels([_t("month_ago"), _t("now_rank")])
        ax.tick_params(axis="x", labeltop=True, labelbottom=False)
        return _svg(fig)


def digest_charts(digest: WeeklyDigest | MonthlyDigest) -> dict[str, Any]:
    """Every chart the template shows, as ``data:`` URIs (``None`` when the section is absent)."""
    end = digest.period.end
    load = digest.load
    out: dict[str, Any] = {
        "recovery": data_uri(recovery_days(digest.recovery))
        if digest.recovery and digest.recovery.days
        else None,
        "ctl": data_uri(ctl_path(load, height_in=1.5 if digest.period.is_break else 1.8))
        if load and (load.ctl_series or load.ctl_plan)
        else None,
    }
    if isinstance(digest, WeeklyDigest):
        highlights = digest.highlights.climbs if digest.highlights else []
        out["load"] = data_uri(load_week(load)) if load and load.daily else None
        out["distribution"] = (
            data_uri(weekly_distribution(digest.distribution)) if digest.distribution else None
        )
        out["goal"] = data_uri(goal_trajectory(digest.goal, end)) if digest.goal else None
        out["climbs"] = [data_uri(climb_history(c, this_key="this_period")) for c in highlights]
        return out
    goal = digest.fitness.goal if digest.fitness else None
    out["consistency"] = (
        data_uri(consistency_weeks(digest.consistency))
        if digest.consistency and digest.consistency.by_week
        else None
    )
    out["power_curve"] = data_uri(power_curve(digest.power_curve)) if digest.power_curve else None
    out["goal"] = data_uri(goal_trajectory(goal, end)) if goal else None
    out["distribution"] = (
        data_uri(monthly_distribution(digest.distribution_by_week))
        if digest.distribution_by_week
        else None
    )
    out["limiters"] = (
        data_uri(limiter_slope(digest.limiters))
        if digest.limiters and digest.limiters.lines
        else None
    )
    return out
