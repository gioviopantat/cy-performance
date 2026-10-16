"""Charts for the per-ride PDF (docs/specs/ride-report.md): matplotlib -> SVG.

Each chart carries our verdict (counted vs excused minutes, goal trajectory, outdoor
discounts, climb history under fatigue/heat); intervals.icu already draws the raw traces.
Charts read only :mod:`cyp.reports.ride_model` fields and compute nothing beyond plotting.

Text is converted to paths (``svg.fonttype = "path"``), so the PDF engine never needs the CJK
font; matplotlib picks the first installed font from :data:`CJK_FONTS`. Colours follow the
reference data-viz palette (light, print): categorical blue / orange / aqua, recessive grey
chrome, a single-hue orange ramp for temperature.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import cache
from importlib import resources
from typing import Any

import matplotlib
import yaml

matplotlib.use("Agg")

import matplotlib.dates as mdates
from matplotlib import font_manager
from matplotlib.axes import Axes
from matplotlib.colors import LinearSegmentedColormap, Normalize, to_rgb
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from cyp.reports.ride_model import (
    ClimbCompare,
    ClimbEffort,
    Goal,
    Intent,
    OutdoorContext,
    RideReport,
    Segment,
    Series,
)

#: zh-TW capable fonts, macOS first, then the Linux (CI) Noto/Source Han names. Same face as
#: the PDF body text (ride.css), so charts and text match.
CJK_FONTS = (
    "Heiti TC",
    "PingFang TC",
    "Noto Sans CJK TC",
    "Noto Sans TC",
    "Source Han Sans TC",
    "Arial Unicode MS",
)
#: Latin face tried before the CJK one: CJK fonts draw ``°`` and ``·`` full-width.
LATIN_FONTS = ("Arial",)

SERIES_1 = "#2a78d6"  # blue: the ride's own measurement
SERIES_2 = "#eb6834"  # orange: target / goal
SERIES_3 = "#1baf7a"  # aqua: counted toward the stimulus
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
SURFACE = "#ffffff"
HEAT = LinearSegmentedColormap.from_list("heat", ["#fbd9c9", "#eb6834", "#8a3110"])

WIDTH_IN = 6.7  # A4 text block (170 mm)
TEXT_FILE = "templates/ride_charts.zh-TW.yaml"


@cache
def _text() -> dict[str, str]:
    raw = resources.files("cyp.reports").joinpath(TEXT_FILE).read_text(encoding="utf-8")
    loaded = yaml.safe_load(raw)
    return {str(k): str(v) for k, v in loaded.items()}


def _t(key: str, **kw: object) -> str:
    """Chart text from the zh-TW strings file (never inline in code)."""
    return _text()[key].format(**kw)


@cache
def cjk_font() -> str | None:
    """The first installed font from :data:`CJK_FONTS`, or ``None`` (zh-TW would be tofu)."""
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((n for n in CJK_FONTS if n in names), None)


def _families() -> list[str]:
    """Font fallback chain: a Latin face, then the zh-TW face, then matplotlib's default."""
    names = {f.name for f in font_manager.fontManager.ttflist}
    cjk = cjk_font()
    return [*(n for n in LATIN_FONTS if n in names), *([cjk] if cjk else []), "DejaVu Sans"]


def _rc() -> Any:  # matplotlib types rc keys as a Literal union
    return {
        "font.family": _families(),
        "font.size": 8,
        "axes.unicode_minus": False,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": INK_2,
        "axes.titlesize": 9,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "legend.frameon": False,
        "legend.fontsize": 7.5,
        "svg.fonttype": "path",
        "svg.hashsalt": "cyp-ride-report",
    }


@contextmanager
def _figure(height_in: float) -> Iterator[tuple[Figure, Axes]]:
    with matplotlib.rc_context(_rc()):
        fig = Figure(figsize=(WIDTH_IN, height_in), facecolor=SURFACE)
        ax = fig.add_subplot()
        _chrome(ax)
        yield fig, ax


def _chrome(ax: Axes) -> None:
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(axis="both", length=0, pad=4)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)


def _svg(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", pad_inches=0.04, metadata={"Date": None})
    return buf.getvalue()


def _dn(day: dt.date) -> float:
    """Matplotlib date number (``date2num`` is untyped)."""
    return float(mdates.date2num(day))  # type: ignore[no-untyped-call]


def data_uri(svg: bytes) -> str:
    """``data:`` URI for an ``<img src>`` in the HTML template."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def _minutes(series: Series | None, values: Sequence[float | None]) -> list[float]:
    step = series.step_s if series else 1
    return [i * step / 60 for i in range(len(values))]


def _span_end_min(series: Series | None, segments: Sequence[Segment]) -> float:
    lengths = [len(series.power_w), len(series.temp_c)] if series else [0]
    from_series = max(lengths) * (series.step_s if series else 0) / 60
    from_segments = max((s.end_s for s in segments), default=0) / 60
    return max(from_series, from_segments, 1.0)


def _plot_gappy(ax: Axes, x: list[float], y: Sequence[float | None], **kw: Any) -> None:
    """Plot with ``None`` as gaps (matplotlib breaks the line at NaN)."""
    ax.plot(x, [float("nan") if v is None else v for v in y], **kw)


def _shade(ax: Axes, segments: Sequence[Segment], *, color: str, hatch: str | None) -> None:
    for s in segments:
        ax.axvspan(
            s.start_s / 60,
            s.end_s / 60,
            facecolor=(*to_rgb(color), 0.18),
            hatch=hatch,
            edgecolor=(*to_rgb(MUTED), 0.7) if hatch else "none",
            linewidth=0,
        )


def _label_segments(ax: Axes, segments: Sequence[Segment], *, top: bool) -> None:
    """Label spans above the plot (``top``) or just inside the baseline; rows never collide."""
    for s in segments:
        if s.label_zh:
            ax.annotate(
                s.label_zh,
                xy=((s.start_s + s.end_s) / 120, 1.0 if top else 0.0),
                xycoords=("data", "axes fraction"),
                xytext=(0, 2 if top else 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=7,
                color=INK_2,
            )


def _unit(ax: Axes, unit: str) -> None:
    """Unit above the y axis, horizontal (a rotated label is harder to read)."""
    ax.set_title(unit, loc="left", color=INK_2, fontsize=7.5, pad=4)


def _no_data(ax: Axes, text: str) -> None:
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=MUTED)


def intent_timeline(intent: Intent, series: Series | None) -> bytes:
    """Section 2: power over time, target band, counted (aqua) and excused (hatched) stretches."""
    with _figure(2.3) as (fig, ax):
        end = _span_end_min(series, [*intent.counted, *intent.excused])
        _shade(ax, intent.counted, color=SERIES_3, hatch=None)
        _shade(ax, intent.excused, color=MUTED, hatch="////")
        handles: list[Any] = []
        if intent.target_low_w is not None and intent.target_high_w is not None:
            ax.axhspan(intent.target_low_w, intent.target_high_w, color=SERIES_2, alpha=0.16, lw=0)
            handles.append(
                Patch(
                    color=SERIES_2,
                    alpha=0.3,
                    label=_t("target_band", low=intent.target_low_w, high=intent.target_high_w),
                )
            )
        has_power = bool(series and any(v is not None for v in series.power_w))
        if series and has_power:
            _plot_gappy(
                ax, _minutes(series, series.power_w), series.power_w, color=SERIES_1, lw=0.9
            )
            handles.insert(0, Line2D([], [], color=SERIES_1, lw=1.5, label=_t("power")))
        else:
            _no_data(ax, _t("no_power"))
        handles += [
            Patch(facecolor=SERIES_3, alpha=0.35, label=_t("counted")),
            Patch(facecolor=MUTED, alpha=0.3, hatch="////", edgecolor=MUTED, label=_t("excused")),
        ]
        _label_segments(ax, intent.counted, top=True)
        _label_segments(ax, intent.excused, top=False)
        ax.set_xlim(0, end)
        ax.set_ylim(bottom=0)
        ax.set_xlabel(_t("minutes"))
        _unit(ax, "W")
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=4)
        return _svg(fig)


def ftp_trajectory(goal: Goal, today: dt.date) -> bytes:
    """Section 3: FTP history, season target, this ride's evidence and the projected date."""
    with _figure(2.4) as (fig, ax):
        handles: list[Any] = []
        if goal.history:
            ax.plot(
                [_dn(p.day) for p in goal.history],
                [p.ftp_w for p in goal.history],
                color=SERIES_1,
                lw=1.5,
                marker="o",
                ms=4,
                mec=SURFACE,
                mew=1,
            )
            handles.append(
                Line2D([], [], color=SERIES_1, lw=1.5, marker="o", label=_t("ftp_history"))
            )
        ax.plot([_dn(today)], [goal.current_ftp_w], "o", color=SERIES_1, ms=7, mec=SURFACE, mew=1.5)
        ax.annotate(
            _t("now", w=f"{goal.current_ftp_w:.0f}"),
            (_dn(today), goal.current_ftp_w),
            xytext=(-6, 8),
            textcoords="offset points",
            ha="right",
            color=INK,
        )
        if goal.this_ride_evidence_w is not None:
            ax.plot(
                [_dn(today)], [goal.this_ride_evidence_w], "D", color=SERIES_3, ms=6, mec=SURFACE
            )
            handles.append(Line2D([], [], color=SERIES_3, marker="D", lw=0, label=_t("evidence")))
        if goal.projected_date is not None:
            ax.plot(
                [_dn(today), _dn(goal.projected_date)],
                [goal.current_ftp_w, goal.target_w],
                color=SERIES_1,
                lw=1.2,
                ls=(0, (1.5, 2)),
            )
            ax.plot(
                [_dn(goal.projected_date)], [goal.target_w], "o", mfc=SURFACE, mec=SERIES_1, ms=6
            )
            ax.annotate(
                _t("projected", date=f"{goal.projected_date:%Y-%m-%d}"),
                (_dn(goal.projected_date), goal.target_w),
                xytext=(0, -14),
                textcoords="offset points",
                ha="center",
                color=INK_2,
            )
            handles.append(
                Line2D([], [], color=SERIES_1, ls=(0, (1.5, 2)), label=_t("projected_path"))
            )
        ax.axhline(goal.target_w, color=SERIES_2, lw=1.2)
        ax.axvline(_dn(goal.target_date), color=AXIS, lw=0.8)
        ax.annotate(
            _t("target", w=f"{goal.target_w:.0f}", date=f"{goal.target_date:%Y-%m-%d}"),
            (_dn(goal.target_date), goal.target_w),
            xytext=(-4, 4),
            textcoords="offset points",
            ha="right",
            va="bottom",
            color=INK,
        )
        handles.append(Line2D([], [], color=SERIES_2, lw=1.2, label=_t("season_target")))
        first = min([today, *(p.day for p in goal.history)])
        last = max(today, goal.target_date, goal.projected_date or goal.target_date)
        ax.set_xlim(
            _dn(first - dt.timedelta(days=14)),
            _dn(last + dt.timedelta(days=21)),
        )
        lows = [goal.current_ftp_w, *(p.ftp_w for p in goal.history)]
        ax.set_ylim(min(lows) - 15, goal.target_w + 12)
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))  # type: ignore[no-untyped-call]
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))  # type: ignore[no-untyped-call]
        _unit(ax, "W")
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=4)
        return _svg(fig)


def outdoor_strip(outdoor: OutdoorContext, series: Series | None) -> bytes:
    """Section 4: stops (hatched) on the time axis, with temperature when it was recorded."""
    has_temp = bool(series and any(v is not None for v in series.temp_c))
    with _figure(1.9 if has_temp else 0.9) as (fig, ax):
        end = _span_end_min(series, outdoor.stops)
        _shade(ax, outdoor.stops, color=MUTED, hatch="////")
        handles: list[Any] = [
            Patch(facecolor=MUTED, alpha=0.3, hatch="////", edgecolor=MUTED, label=_t("stop"))
        ]
        if series and has_temp:
            _plot_gappy(ax, _minutes(series, series.temp_c), series.temp_c, color=SERIES_2, lw=1.2)
            handles.insert(0, Line2D([], [], color=SERIES_2, lw=1.5, label=_t("temp")))
            _unit(ax, "°C")
        else:
            ax.set_yticks([])
            ax.grid(False)
            if not outdoor.stops:
                _no_data(ax, _t("no_outdoor"))
        ax.set_xlim(0, end)
        ax.set_xlabel(_t("minutes"))
        ax.legend(
            handles=handles,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.4 if has_temp else -0.9),
            ncol=2,
        )
        return _svg(fig)


def _climb_metric(climb: ClimbCompare) -> tuple[str, list[float]]:
    """Pick the one y measure every effort has: W, then W/kg, then minutes."""
    efforts = [climb.this, *climb.history]
    if all(e.power_w is not None for e in efforts):
        return "W", [e.power_w or 0.0 for e in efforts]
    if all(e.w_kg is not None for e in efforts):
        return "W/kg", [e.w_kg or 0.0 for e in efforts]
    return _t("minutes"), [e.time_s / 60 for e in efforts]


def _kj_size(kj: float | None) -> float:
    return 30.0 if kj is None else 20.0 + min(kj, 1500.0) / 1500.0 * 160.0


def climb_history(climb: ClimbCompare) -> bytes:
    """Section 5: each ascent vs date; size = kJ already spent, colour = temperature."""
    efforts: list[ClimbEffort] = [climb.this, *climb.history]
    unit, ys = _climb_metric(climb)
    temps = [e.temp_c for e in efforts if e.temp_c is not None]
    norm = Normalize(min(temps, default=20.0) - 1, max(temps, default=30.0) + 1)
    with _figure(2.2) as (fig, ax):
        for i, (e, y) in enumerate(zip(efforts, ys, strict=True)):
            colour = MUTED if e.temp_c is None else HEAT(norm(e.temp_c))
            ax.scatter(
                [_dn(e.day)],
                [y],
                s=_kj_size(e.kj_before),
                color=colour,
                edgecolors=INK if i == 0 else SURFACE,
                linewidths=1.6 if i == 0 else 1.0,
                zorder=3 if i == 0 else 2,
            )
        ax.annotate(
            _t("today"),
            (_dn(climb.this.day), ys[0]),
            xytext=(0, 9),
            textcoords="offset points",
            ha="center",
            color=INK,
        )
        days = [e.day for e in efforts]
        ax.set_xlim(
            _dn(min(days) - dt.timedelta(days=20)),
            _dn(max(days) + dt.timedelta(days=20)),
        )
        spread = (max(ys) - min(ys)) or max(abs(ys[0]) * 0.05, 0.5)
        ax.set_ylim(min(ys) - spread * 0.4, max(ys) + spread * 0.6)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=6))  # type: ignore[no-untyped-call]
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))  # type: ignore[no-untyped-call]
        _unit(ax, unit)
        handles: list[Any] = [
            Line2D(
                [], [], lw=0, marker="o", mfc=SURFACE, mec=INK, mew=1.6, ms=7, label=_t("today")
            ),
        ]
        handles += [
            Line2D(
                [],
                [],
                lw=0,
                marker="o",
                color=MUTED,
                ms=_kj_size(kj) ** 0.5,
                label=_t("kj_before", kj=kj),
            )
            for kj in (200, 600, 1200)
        ]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=4)
        if temps:
            sm = matplotlib.cm.ScalarMappable(norm=norm, cmap=HEAT)
            bar = fig.colorbar(sm, ax=ax, pad=0.01, fraction=0.03, aspect=18)
            bar.ax.set_title(_t("temp_axis"), color=INK_2, fontsize=7.5, loc="left")
            bar.outline.set_visible(False)
            bar.ax.tick_params(length=0, labelcolor=INK_2)
        return _svg(fig)


def report_charts(report: RideReport) -> dict[str, Any]:
    """Every chart the template shows, as ``data:`` URIs (``None`` when the section is absent)."""
    today = report.ride.start_local.date()
    return {
        "intent": data_uri(intent_timeline(report.intent, report.series))
        if report.intent
        else None,
        "goal": data_uri(ftp_trajectory(report.goal, today)) if report.goal else None,
        "outdoor": data_uri(outdoor_strip(report.outdoor, report.series))
        if report.outdoor
        else None,
        "climbs": [data_uri(climb_history(c)) for c in report.climbs],
    }
