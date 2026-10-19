"""Section 3 of the per-ride report: distance to the season FTP target.

Current FTP is the setting effective on the ride's day; the trajectory comes from the FTP
status' estimate series (:func:`cyp.analysis.longitudinal.goal.project_goal`). This ride's FTP
evidence reuses :func:`cyp.analysis.longitudinal.ftp.max_effort_evidence` (already in the FTP
status): only a near-maximal sustained attempt (≥ 15 min at ≥ 100 % FTP, a 20-min effort or a
test) is evidence, converted with Coggan's 95 % rule.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal.ftp import FtpStatus, compute_ftp_status
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.dataset import Dataset
from cyp.reports.ride_common import METHOD_DOCS, RideInputs, text
from cyp.reports.ride_model import FtpPoint, Goal

#: Coggan: FTP ≈ 95 % of a maximal 20-minute effort (also applied to a ≥ 15-min maximal climb).
FTP_FROM_20MIN = 0.95


def _prev_ride_day(ds: Dataset, a_id: int, day: dt.date) -> dt.date | None:
    days = [r.date for r in ds.rides(None, day) if r.id != a_id and r.date <= day]
    return max(days) if days else None


def ride_evidence(inp: RideInputs, status: FtpStatus) -> tuple[float, str] | None:
    """(FTP evidence W, zh label of the effort) from this ride's near-maximal attempts."""
    row, ftp = inp.row, status.current_ftp
    if row is None or not ftp:
        return None
    best: tuple[float, str] | None = None
    for item in status.max_efforts.sustained:
        if item.get("activity_id") != row.id:
            continue
        watts: float | None = None
        if item.get("kind") == "power_20min":
            watts = float(item["watts"])
        elif item.get("kind") == "test" and row.power_ok:
            watts = row.power_curve.get(1200)
        elif item.get("kind") == "climb" and item.get("pct_ftp"):
            watts = float(item["pct_ftp"]) / 100 * float(ftp)
        if watts:
            ev = round(watts * FTP_FROM_20MIN, 1)
            if best is None or ev > best[0]:
                best = (ev, str(item.get("label_zh", "")))
    return best


def _project(
    status: FtpStatus, day: dt.date, target_w: float, target_date: dt.date
) -> goal_mod.GoalProjection:
    return goal_mod.project_goal(
        status.estimates,
        as_of=day,
        target_w=target_w,
        target_date=target_date,
        setting_w=float(status.current_ftp) if status.current_ftp else None,
    )


def build_goal(inp: RideInputs) -> Goal | None:
    """Section 3 (``None`` without an FTP goal, a current FTP or the FTP status)."""
    ds, cfg, day, trends = inp.ds, inp.cfg, inp.day, inp.trends
    if ds is None or cfg is None or trends is None:
        return None
    status = trends.ftp
    g = next((g for g in cfg.goals if g.kind == "ftp_target"), None)
    target = g.target.get("ftp") if g else None
    if g is None or not target or not status.current_ftp:
        return None
    target_w = float(target)
    setting = float(status.current_ftp)
    proj = _project(status, day, target_w, g.date)
    prev_day = _prev_ride_day(ds, inp.activity.id, day)
    prev = None
    if prev_day is not None:
        prev_status = status if prev_day == day else compute_ftp_status(ds, prev_day)
        prev = _project(prev_status, prev_day, target_w, g.date)
    shift = (
        (proj.projected_date - prev.projected_date).days
        if prev is not None and prev.projected_date and proj.projected_date
        else None
    )
    evidence = ride_evidence(inp, status)
    settings_row = ds.settings_on(day)
    since = min(cfg.season.start, day - dt.timedelta(days=7 * goal_mod.TREND_WEEKS))
    history = [
        FtpPoint(day=s.effective_from, ftp_w=float(s.ftp), source=f"setting:{s.source}")
        for s in ds.settings
        if s.ftp and since <= s.effective_from <= day
    ] + [FtpPoint(day=d, ftp_w=round(v, 1), source=status.estimate_source) for d, v in proj.weekly]
    history.sort(key=lambda p: (p.day, p.source))
    if proj.projected_date is not None:
        on_track: bool | None = proj.projected_date <= g.date
    else:
        on_track = False if proj.after_target else None
    level = float(round(proj.level_w)) if proj.level_w is not None else None

    nums: dict[str, Any] = {
        "min_weeks": goal_mod.MIN_WEEKS,
        "min_points": goal_mod.MIN_RAW_POINTS,
        "min_increases": goal_mod.MIN_WEEKLY_INCREASES,
        "min_slope": goal_mod.MIN_SLOPE_W_PER_WEEK,
        "min_tau": goal_mod.MIN_KENDALL_TAU,
        "max_days": goal_mod.MAX_PROJECTION_DAYS,
        "after_weeks": goal_mod.AFTER_TARGET_WEEKS,
        "below": goal_mod.MAX_BELOW_SETTING,
        "weeks": len(proj.weekly),
        "points": len(proj.raw),
        "increases": proj.weekly_increases or 0,
        "slope": proj.slope_w_per_week or 0.0,
        "recent": proj.recent_slope_w_per_week or 0.0,
        "tau": proj.kendall_tau or 0.0,
        "level": level or 0.0,
        "target": target_w,
        "target_date": g.date,
        "setting": setting,
        "gap": 1 - (level or 0.0) / setting,
    }
    reasons = [
        Reason(
            text_zh=text("goal.source", source=status.estimate_source),
            evidence={"estimate_source": status.estimate_source},
        )
    ]
    if proj.slope_w_per_week is not None:
        reasons.append(
            Reason(
                text_zh=text("goal.trend", **nums),
                evidence={
                    "weeks": len(proj.weekly),
                    "raw_points": len(proj.raw),
                    "slope_w_per_week": proj.slope_w_per_week,
                    "recent_slope_w_per_week": proj.recent_slope_w_per_week,
                    "weekly_increases": proj.weekly_increases,
                    "kendall_tau": proj.kendall_tau,
                    "level_w": level,
                },
            )
        )
    if not status.max_efforts.sustained:
        reasons.append(
            Reason(
                text_zh=text("goal.no_max_effort", window=status.max_efforts.window_days),
                evidence={"n_sustained_efforts": 0},
            )
        )
    if proj.reason is not None:
        reasons.append(Reason(text_zh=text(f"goal.{proj.reason}", **nums), evidence={}))
    elif level is not None and level >= target_w:
        reasons.append(Reason(text_zh=text("goal.reached", **nums), evidence={}))
    if proj.projected_date is not None:
        key = "goal.on_track" if on_track else "goal.off_track"
        reasons.append(
            Reason(
                text_zh=text(key, date=proj.projected_date, target_date=g.date),
                evidence={
                    "projected_date": proj.projected_date.isoformat(),
                    "target_date": g.date.isoformat(),
                },
            )
        )
    if prev is not None and prev_day is not None and shift is not None:
        shift_text = (
            text("goal.shift_earlier", days=-shift)
            if shift < 0
            else text("goal.shift_later", days=shift)
            if shift > 0
            else text("goal.shift_same")
        )
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.shift",
                    prev_day=prev_day,
                    prev_date=prev.projected_date,
                    date=proj.projected_date,
                    shift_text=shift_text,
                ),
                evidence={"prev_day": prev_day.isoformat(), "shift_days": shift},
            )
        )
    if evidence is not None:
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.evidence", label=evidence[1], ev=evidence[0], frac=FTP_FROM_20MIN
                ),
                evidence={"ftp_evidence_w": evidence[0]},
            )
        )
    reasons.append(Reason(text_zh=text("goal.method", **nums), evidence={}))
    if proj.projected_date:
        headline = text(
            "goal.headline_date", weeks=len(proj.weekly), date=proj.projected_date, target=target_w
        )
    elif proj.after_target:
        headline = text("goal.headline_after", **nums)
    else:
        headline = text("goal.headline", current=setting, target=target_w, target_date=g.date)
    expl = Explanation(
        key="ride.goal",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id=goal_mod.GOAL_VERSION,
            version=goal_mod.GOAL_VERSION,
            inputs={
                "trend_weeks": goal_mod.TREND_WEEKS,
                "min_weeks": goal_mod.MIN_WEEKS,
                "min_raw_points": goal_mod.MIN_RAW_POINTS,
                "min_weekly_increases": goal_mod.MIN_WEEKLY_INCREASES,
                "min_slope_w_per_week": goal_mod.MIN_SLOPE_W_PER_WEEK,
                "min_kendall_tau": goal_mod.MIN_KENDALL_TAU,
                "max_below_setting": goal_mod.MAX_BELOW_SETTING,
                "after_target_weeks": goal_mod.AFTER_TARGET_WEEKS,
                "max_projection_days": goal_mod.MAX_PROJECTION_DAYS,
                "target_w": target_w,
                "as_of": day.isoformat(),
            },
            doc=METHOD_DOCS[goal_mod.GOAL_VERSION],
        ),
        confidence="medium" if proj.projected_date and status.max_efforts.sustained else "low",
        glossary_terms=["ftp_target_season", "eftp"],
    )
    return Goal(
        target_w=target_w,
        target_date=g.date,
        current_ftp_w=setting,
        current_source=settings_row.source if settings_row else "unknown",
        trend_level_w=level,
        this_ride_evidence_w=evidence[0] if evidence else None,
        projected_date=proj.projected_date,
        projected_shift_days=shift,
        on_track=on_track,
        history=history,
        explanation=expl.to_json_dict(),
    )
