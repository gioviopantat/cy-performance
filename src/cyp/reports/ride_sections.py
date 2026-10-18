"""Sections 4-7 of the per-ride report plus the chart series.

- 4 outdoor context: :mod:`cyp.analysis.ride.context`, heat judged against the athlete's usual.
- 5 climbs: the repeat-climb leaderboard plus kJ already spent and temperature per ascent.
- 6 feel: RPE / feel against :func:`cyp.analysis.readiness.expected_rpe`.
- 7 limiters: minutes this ride and this ISO week spent in each detected limiter's power range.
- series: 10 s block means of power, HR, altitude and temperature.

Displayed numbers are rounded once, here, and the texts quote the stored values.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from cyp.analysis.longitudinal import climbs as climbs_mod
from cyp.analysis.longitudinal.durability import LATE_KJ
from cyp.analysis.readiness import RPE_SURPRISE, YesterdayRide, expected_rpe
from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride.classify import CLASS_ZH
from cyp.analysis.ride.frames import FloatArray, RideFrame
from cyp.core.timeutil import week_start
from cyp.dataset import ActivityRow
from cyp.reports.ride_common import RideInputs, join, text, texts
from cyp.reports.ride_model import (
    ClimbCompare,
    ClimbEffort,
    Feel,
    LimiterLine,
    Limiters,
    OutdoorContext,
    Segment,
    Series,
)
from cyp.store.models import Activity

#: Downsampling step (s) of the chart series: ~700 points for a 2-hour ride.
SERIES_STEP_S = 10
#: Days of past outdoor rides whose median temperature is the athlete's "usual".
HEAT_BASELINE_DAYS = 28
#: Fewer rides with a temperature stream than this is no baseline (absolute floor only).
HEAT_BASELINE_MIN_RIDES = 3
#: Past ascents shown per climb (the most recent ones, oldest first).
CLIMB_HISTORY_MAX = 10
#: Work done before a climb (kJ per kg) that makes it a different effort: ~4 kJ/kg is about an
#: hour of Z2; durability work shows power at a given effort falls with prior work done.
CLIMB_FATIGUE_KJ_PER_KG = 4.0
#: Temperature difference (°C) from which heat is worth naming when comparing ascents.
CLIMB_HEAT_MARGIN_C = 5.0
#: |RPE − expected| from which feel is called harder / easier than expected (half of the
#: readiness rule's :data:`~cyp.analysis.readiness.RPE_SURPRISE`, which caps tomorrow at EASY).
FEEL_GAP_NOTABLE = 1.5
#: 30 s power ranges (fraction of FTP, upper bound open when ``None``) whose minutes work on each
#: limiter: VO2 at ≥ 106 % (Coggan Z5+); sustained power from sweet spot (88 %) to threshold
#: (105 %); aerobic volume in Z2 (56-75 %); grey zone is tempo below sweet spot (76-88 %), where
#: fewer minutes is the goal. ``durability`` is moving minutes after
#: :data:`~cyp.analysis.longitudinal.durability.LATE_KJ` kJ instead.
LIMITER_RANGES: dict[str, tuple[float, float | None]] = {
    "vo2_ceiling": (1.06, None),
    "sustained_power": (0.88, 1.05),
    "aerobic_volume": (0.56, 0.76),
    "grey_zone": (0.76, 0.88),
}
#: Limiters whose minutes should go down, not up.
FEWER_IS_BETTER = frozenset({"grey_zone"})


def _median(vals: Iterable[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return float(statistics.median(v)) if v else None


def _temp_avg(frame: RideFrame | None) -> float | None:
    return ctx_mod.temperature(frame)[0] if frame is not None else None


# ------------------------------------------------------------------------------- outdoor


def usual_temp(inp: RideInputs) -> float | None:
    """Median ride temperature of the athlete's outdoor rides in the last weeks."""
    if inp.ds is None:
        return None
    lo = inp.day - dt.timedelta(days=HEAT_BASELINE_DAYS)
    temps = []
    for r in inp.ds.rides(lo, inp.day - dt.timedelta(days=1)):
        a = inp.session.get(Activity, r.id)
        if a is None or a.trainer or r.sport == "VirtualRide":
            continue
        t = _temp_avg(inp.frame_of(r.id))
        if t is not None:
            temps.append(t)
    return float(statistics.median(temps)) if len(temps) >= HEAT_BASELINE_MIN_RIDES else None


def build_outdoor(inp: RideInputs, *, planned_reps: bool) -> OutdoorContext | None:
    """Section 4 (``None`` indoors or without streams)."""
    frame = inp.frame
    if inp.indoor or frame is None:
        return None
    f = ctx_mod.outdoor_context(frame, usual_temp(inp))
    caveats = []
    for c in f.caveats:
        line = text(f"outdoor.{c.code}", **c.evidence)
        if c.code == "stops" and planned_reps:
            line += text("outdoor.stops_reps")
        caveats.append(line)
    return OutdoorContext(
        stops_n=len(f.stops),
        stopped_min=float(round(f.stopped_s / 60)),
        coasting_pct=float(round(f.coasting_pct)) if f.coasting_pct is not None else None,
        temp_avg_c=float(round(f.temp_avg_c)) if f.temp_avg_c is not None else None,
        temp_max_c=float(round(f.temp_max_c)) if f.temp_max_c is not None else None,
        stops=[
            Segment(start_s=s.start_s, end_s=s.end_s, label_zh=text("outdoor.stop", sec=s.seconds))
            for s in f.stops
        ],
        caveats_zh=caveats,
    )


# ------------------------------------------------------------------------------- climbs


def _climb_json(row: ActivityRow) -> list[dict[str, Any]]:
    cl = list(row.climbs)
    return climbs_mod.strip_power(cl) if row.power_unreliable is not None else cl


def _effort(inp: RideInputs, row: ActivityRow, c: Mapping[str, Any]) -> ClimbEffort:
    frame = inp.frame_of(row.id)
    start, dur = int(c.get("start_s") or 0), int(c["duration_s"])
    watts = inp.power_of(row)
    kj = None
    if watts is not None and start <= watts.size:
        kj = float(round(float(np.nansum(watts[:start])) / 1000))
    temp = None
    t = frame.col("temp_c") if frame is not None else None
    if t is not None:
        seg = t[start : start + dur + 1]
        seg = seg[np.isfinite(seg)]
        temp = float(round(float(seg.mean()))) if seg.size else None
    power = c.get("avg_w")
    return ClimbEffort(
        day=row.date,
        power_w=float(round(power)) if isinstance(power, int | float) else None,
        w_kg=c.get("w_kg"),
        time_s=dur,
        kj_before=kj,
        temp_c=temp,
    )


def build_climbs(inp: RideInputs) -> list[ClimbCompare]:
    """Section 5: each fingerprinted climb of this ride that has at least one past ascent."""
    row, ds = inp.row, inp.ds
    if ds is None or row is None or not row.climbs:
        return []
    rides = [r for r in ds.rides(None, row.date) if r.id == row.id or r.date <= row.date]
    by_id = {r.id: r for r in rides}
    boards = {
        b.fingerprint: b
        for b in climbs_mod.leaderboard((r.id, r.date, _climb_json(r)) for r in rides)
    }
    out: list[ClimbCompare] = []
    for c in _climb_json(row):
        fp, dur = c.get("fingerprint"), c.get("duration_s")
        board = boards.get(fp) if fp else None
        if board is None or not isinstance(dur, int | float):
            continue
        rank = next(
            (
                i
                for i, e in enumerate(board.efforts, start=1)
                if e.activity_id == row.id and e.duration_s == int(dur)
            ),
            None,
        )
        past = sorted(
            (e for e in board.efforts if e.activity_id != row.id),
            key=lambda e: (e.date, e.activity_id),
            reverse=True,
        )[:CLIMB_HISTORY_MAX]
        if not past:
            continue
        history = []
        for e in sorted(past, key=lambda e: (e.date, e.activity_id)):
            r = by_id[e.activity_id]
            src = next(
                (
                    x
                    for x in _climb_json(r)
                    if x.get("fingerprint") == fp and x.get("duration_s") == e.duration_s
                ),
                None,
            )
            if src is not None:
                history.append(_effort(inp, r, src))
        this = _effort(inp, row, c)
        out.append(
            ClimbCompare(
                name=text(
                    "climb.name",
                    km=(c.get("distance_m") or 0) / 1000,
                    grade=c.get("avg_grade_pct") or 0.0,
                ),
                distance_km=round((c.get("distance_m") or 0) / 1000, 2) or None,
                grade_pct=c.get("avg_grade_pct"),
                this=this,
                history=history,
                rank=rank,
                of_n=len(board.efforts),
                note_zh=_climb_note(this, history, rank, len(board.efforts), ds.weight_kg),
            )
        )
    return out


def _climb_note(
    this: ClimbEffort,
    history: list[ClimbEffort],
    rank: int | None,
    n: int,
    weight_kg: float | None,
) -> str:
    parts: list[str] = []
    if rank is not None:
        parts.append(text("climb.rank", rank=rank, of_n=n))
    t_med = _median(e.time_s for e in history)
    if t_med is not None and round(this.time_s - t_med):
        diff = round(this.time_s - t_med)
        parts.append(
            text(
                "climb.time",
                dir=text("climb.faster" if diff < 0 else "climb.slower"),
                sec=abs(diff),
            )
        )
    w_med = _median(e.power_w for e in history)
    if this.power_w is not None and w_med is not None:
        parts.append(text("climb.power", w=this.power_w, hist_w=w_med))
    kj_med = _median(e.kj_before for e in history)
    if this.kj_before is not None and kj_med is not None and weight_kg:
        d = (this.kj_before - kj_med) / weight_kg
        nums = {"kj": this.kj_before, "diff": abs(d), "margin": CLIMB_FATIGUE_KJ_PER_KG}
        if d >= CLIMB_FATIGUE_KJ_PER_KG:
            parts.append(text("climb.fatigue", **nums))
        elif -d >= CLIMB_FATIGUE_KJ_PER_KG:
            parts.append(text("climb.fresh", **nums))
    t_hist = _median(e.temp_c for e in history)
    if (
        this.temp_c is not None
        and t_hist is not None
        and this.temp_c - t_hist >= CLIMB_HEAT_MARGIN_C
    ):
        parts.append(
            text(
                "climb.heat",
                temp=this.temp_c,
                diff=this.temp_c - t_hist,
                margin=CLIMB_HEAT_MARGIN_C,
            )
        )
    return join(parts)


# ------------------------------------------------------------------------------- feel


def build_feel(
    inp: RideInputs, feedback: tuple[float | None, int | None] | None, rate_url: str | None
) -> Feel:
    """Section 6: RPE / feel against the expected RPE of this class and length."""
    a, fact = inp.activity, inp.fact
    rpe: float | None = a.icu_rpe
    feel: int | None = a.feel
    if feedback is not None:
        rpe = feedback[0] if feedback[0] is not None else rpe
        feel = feedback[1] if feedback[1] is not None else feel
    if rpe is None and feel is None:
        return Feel(rated=False, text_zh=text("feel.unrated"), rate_url=rate_url)
    cls = fact["classification"]
    minutes = float(fact["moving_min"])
    expected = round(
        expected_rpe(YesterdayRide(rated_classification=cls, rated_minutes=minutes)), 1
    )
    parts: list[str] = []
    gap = None
    if rpe is not None:
        gap = round(float(rpe) - expected, 1)
        vals = {
            "rpe": float(rpe),
            "expected": expected,
            "gap": abs(gap),
            "cls": CLASS_ZH.get(cls or "", cls or "?"),
            "minutes": minutes,
            "surprise": RPE_SURPRISE,
            "notable": FEEL_GAP_NOTABLE,
        }
        if gap >= RPE_SURPRISE:
            parts.append(text("feel.much_harder", **vals))
        elif gap >= FEEL_GAP_NOTABLE:
            parts.append(text("feel.harder", **vals))
        elif gap <= -FEEL_GAP_NOTABLE:
            parts.append(text("feel.easier", **vals))
        else:
            parts.append(text("feel.as_expected", **(vals | {"gap": gap})))
    if feel is not None:
        key = "feel_strong" if feel <= 2 else "feel_normal" if feel == 3 else "feel_weak"
        parts.append(text(f"feel.{key}", feel=feel))
    return Feel(
        rated=True,
        rpe=float(rpe) if rpe is not None else None,
        feel=feel,
        expected_rpe=expected if rpe is not None else None,
        gap=gap,
        text_zh=join(parts),
        rate_url=rate_url,
    )


# ------------------------------------------------------------------------------- limiters


def limiter_minutes(
    lid: str, watts: FloatArray | None, moving: np.ndarray, ftp: float | None
) -> float:
    """Moving minutes one ride spent on limiter ``lid`` (see :data:`LIMITER_RANGES`)."""
    if watts is None:
        return 0.0
    if lid == "durability":
        late = np.cumsum(np.nan_to_num(watts, nan=0.0)) / 1000 >= LATE_KJ
        return float(np.count_nonzero(late & moving)) / 60
    rng = LIMITER_RANGES.get(lid)
    if rng is None or not ftp:
        return 0.0
    p = np.convolve(np.nan_to_num(watts, nan=0.0), np.ones(30) / 30, mode="same")
    mask = moving & (p >= rng[0] * ftp)
    if rng[1] is not None:
        mask &= p < rng[1] * ftp
    return float(np.count_nonzero(mask)) / 60


def _ride_minutes(inp: RideInputs, r: ActivityRow, lid: str) -> float:
    frame = inp.frame if r.id == inp.activity.id else inp.frame_of(r.id)
    watts = inp.power if r.id == inp.activity.id else inp.power_of(r)
    if frame is None or inp.ds is None:
        return 0.0
    return limiter_minutes(lid, watts, frame.moving, inp.ds.ftp_on(r.date))


def build_limiters(inp: RideInputs) -> Limiters | None:
    """Section 7: minutes this ride and this ISO week spent on each detected limiter."""
    trends, row, ds = inp.trends, inp.row, inp.ds
    if trends is None or row is None or ds is None:
        return None
    names: dict[str, str] = texts()["limiter_names"]
    detected = [lim for lim in trends.report.limiters if str(lim["id"]) in names]
    checks = trends.report.limiter_checks
    if not detected:
        if checks:
            listed = text("common.list_sep").join(
                names.get(str(c["id"]), str(c["id"])) for c in checks
            )
            return Limiters(
                lines=[], text_zh=text("limiters.insufficient", n=len(checks), names=listed)
            )
        return Limiters(lines=[], text_zh=text("limiters.none"))
    week = list(ds.rides(week_start(row.date), row.date))
    lines = []
    for lim in detected:
        lid = str(lim["id"])
        name = names[lid] + (text("limiters.fewer_suffix") if lid in FEWER_IS_BETTER else "")
        lines.append(
            LimiterLine(
                limiter_id=lid,
                name_zh=name,
                trained_min=float(round(_ride_minutes(inp, row, lid))),
                week_min=float(round(sum(_ride_minutes(inp, r, lid) for r in week))),
            )
        )
    top = lines[0]
    nums = {"name": top.name_zh, "minutes": top.trained_min, "week": top.week_min}
    if top.limiter_id in FEWER_IS_BETTER:
        key = "limiters.fewer"
    else:
        key = "limiters.trained" if top.trained_min >= 1 else "limiters.not_trained"
    return Limiters(lines=lines, text_zh=text(key, **nums))


# ------------------------------------------------------------------------------- series


def _down(x: FloatArray | None, n_out: int, step_s: int) -> list[float | None]:
    if x is None:
        return [None] * n_out
    out: list[float | None] = []
    for i in range(n_out):
        b = x[i * step_s : (i + 1) * step_s]
        ok = b[np.isfinite(b)]
        out.append(round(float(ok.mean()), 1) if ok.size else None)
    return out


def build_series(frame: RideFrame | None, step_s: int = SERIES_STEP_S) -> Series | None:
    """Block means every ``step_s`` seconds (``None`` for a block without data)."""
    if frame is None:
        return None
    n_out = -(-frame.n // step_s)
    return Series(
        step_s=step_s,
        power_w=_down(frame.watts, n_out, step_s),
        hr_bpm=_down(frame.hr, n_out, step_s),
        altitude_m=_down(frame.alt, n_out, step_s),
        temp_c=_down(frame.col("temp_c"), n_out, step_s),
    )
