"""Digest sections about where the athlete stands: goal, recovery, and what comes next.

- goal (weekly 5 / monthly 5): the FTP target projected now vs at the period start, with
  :func:`cyp.analysis.longitudinal.goal.project_goal` on the FTP status as of each day, and the
  period's best near-maximal effort as FTP evidence (:func:`~cyp.reports.ride_goal.ride_evidence`).
- recovery (weekly 6 / monthly 8): the 7-day HRV vs its baseline band (readiness v2's
  :func:`~cyp.analysis.readiness.hrv_window`, ADR-0013), resting HR, sleep, ride feel vs the
  expected RPE (:func:`~cyp.reports.ride_sections.build_feel`) and the readiness verdicts the
  athlete got each morning (``readiness_daily``).
- next plan (weekly 8 / monthly 9): the stored planned workouts after the period and the season
  weeks they fall in.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from cyp.analysis import readiness as readiness_mod
from cyp.analysis import readiness_job
from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal.ftp import FtpStatus
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.season import PHASE_ZH
from cyp.planning.templates import CEILING_INTENTS, load_library
from cyp.reports import ride_common
from cyp.reports.digest_common import DigestInputs, days, join, text
from cyp.reports.digest_model import GoalProgress, NextPlan, PlannedSession, Recovery, RecoveryDay
from cyp.reports.digest_sections import day_plans
from cyp.reports.ride_goal import FTP_FROM_20MIN, ride_evidence
from cyp.reports.ride_model import FtpPoint
from cyp.reports.ride_sections import build_feel
from cyp.store.models import PlannedWorkout, ReadinessDaily
from cyp.store.repo.planned import is_live

#: Days before the period whose mean resting HR / sleep is the athlete's usual.
BASELINE_DAYS = 28
#: Readiness recommendations, most cautious first (``readiness_counts`` keys).
RECOMMENDATIONS = ("REST", "EASY", "AS_PLANNED", "UPGRADE")


# ------------------------------------------------------------------------------- goal


def _project(status: FtpStatus, day: dt.date, target_w: float, target_date: dt.date) -> Any:
    return goal_mod.project_goal(
        status.estimates,
        as_of=day,
        target_w=target_w,
        target_date=target_date,
        setting_w=float(status.current_ftp) if status.current_ftp else None,
    )


def _evidence(inp: DigestInputs, status: FtpStatus, start: dt.date, end: dt.date) -> Any:
    best: tuple[float, str, dt.date] | None = None
    for row in inp.ds.rides(start, end):
        ev = ride_evidence(inp.ride(row), status)
        if ev is not None and (best is None or ev[0] > best[0]):
            best = (ev[0], ev[1], row.date)
    return best


def build_goal(inp: DigestInputs, start: dt.date, end: dt.date) -> GoalProgress | None:
    """FTP goal now (``end``) vs the day before the period (``None`` without goal / FTP)."""
    cfg = inp.cfg
    g = next((g for g in cfg.goals if g.kind == "ftp_target"), None) if cfg else None
    target = g.target.get("ftp") if g else None
    now, before = inp.trends_at(end), inp.trends_at(start - dt.timedelta(days=1))
    if cfg is None or g is None or not target or now is None or not now.ftp.current_ftp:
        return None
    status, target_w = now.ftp, float(target)
    setting = float(status.current_ftp or 0)
    proj = _project(status, end, target_w, g.date)
    prev = _project(before.ftp, start - dt.timedelta(days=1), target_w, g.date) if before else None
    shift = (
        (proj.projected_date - prev.projected_date).days
        if prev is not None and prev.projected_date and proj.projected_date
        else None
    )
    evidence = _evidence(inp, status, start, end)
    since = min(cfg.season.start, end - dt.timedelta(days=7 * goal_mod.TREND_WEEKS))
    history = [
        FtpPoint(day=s.effective_from, ftp_w=float(s.ftp), source=f"setting:{s.source}")
        for s in inp.ds.settings
        if s.ftp and since <= s.effective_from <= end
    ] + [FtpPoint(day=d, ftp_w=round(v, 1), source=status.estimate_source) for d, v in proj.weekly]
    history.sort(key=lambda p: (p.day, p.source))
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
            text_zh=ride_common.text("goal.source", source=status.estimate_source),
            evidence={"estimate_source": status.estimate_source},
        )
    ]
    if proj.slope_w_per_week is not None:
        reasons.append(
            Reason(
                text_zh=ride_common.text("goal.trend", **nums),
                evidence={
                    "weeks": len(proj.weekly),
                    "slope_w_per_week": proj.slope_w_per_week,
                    "recent_slope_w_per_week": proj.recent_slope_w_per_week,
                    "kendall_tau": proj.kendall_tau,
                    "level_w": level,
                },
            )
        )
    if proj.reason is not None:
        reasons.append(Reason(text_zh=ride_common.text(f"goal.{proj.reason}", **nums)))
    elif level is not None and level >= target_w:
        reasons.append(Reason(text_zh=ride_common.text("goal.reached", **nums)))
    if proj.projected_date is not None:
        key = "goal.on_track" if proj.projected_date <= g.date else "goal.off_track"
        reasons.append(
            Reason(
                text_zh=ride_common.text(key, date=proj.projected_date, target_date=g.date),
                evidence={"projected_date": proj.projected_date.isoformat()},
            )
        )
    if prev is not None and shift is not None:
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.shift",
                    before=start - dt.timedelta(days=1),
                    prev_date=prev.projected_date,
                    end=end,
                    date=proj.projected_date,
                    shift=_shift_text(shift),
                ),
                evidence={"shift_days": shift},
            )
        )
    if evidence is not None:
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.evidence",
                    day=evidence[2],
                    label=evidence[1],
                    frac=FTP_FROM_20MIN,
                    ev=evidence[0],
                ),
                evidence={"ftp_evidence_w": evidence[0], "day": evidence[2].isoformat()},
            )
        )
    elif not status.max_efforts.sustained:
        reasons.append(
            Reason(
                text_zh=ride_common.text(
                    "goal.no_max_effort", window=status.max_efforts.window_days
                ),
                evidence={"n_sustained_efforts": 0},
            )
        )
    reasons.append(Reason(text_zh=ride_common.text("goal.method", **nums)))
    if proj.projected_date is not None:
        headline = ride_common.text(
            "goal.headline_date", weeks=len(proj.weekly), date=proj.projected_date, target=target_w
        )
        if shift is not None:
            headline += text("goal.headline_shift", shift=_shift_text(shift))
    elif proj.after_target:
        headline = ride_common.text("goal.headline_after", **nums)
    else:
        headline = ride_common.text(
            "goal.headline", current=setting, target=target_w, target_date=g.date
        )
    expl = Explanation(
        key=f"digest.goal.{end.isoformat()}",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id=goal_mod.GOAL_VERSION,
            version=goal_mod.GOAL_VERSION,
            inputs={
                "trend_weeks": goal_mod.TREND_WEEKS,
                "target_w": target_w,
                "as_of": end.isoformat(),
                "before": (start - dt.timedelta(days=1)).isoformat(),
            },
            doc=ride_common.METHOD_DOCS[goal_mod.GOAL_VERSION],
            claim="goal_projection.model",
        ),
        confidence="medium" if proj.projected_date and status.max_efforts.sustained else "low",
        glossary_terms=["ftp_target_season", "eftp"],
    )
    return GoalProgress(
        target_w=target_w,
        target_date=g.date,
        current_ftp_w=setting,
        trend_level_w=level,
        projected_date=proj.projected_date,
        projected_date_before=prev.projected_date if prev is not None else None,
        evidence_w=evidence[0] if evidence else None,
        history=history,
        text_zh=headline,
        explanation=expl.to_json_dict(),
    )


def _shift_text(shift: int) -> str:
    if shift < 0:
        return text("goal.earlier", days=-shift)
    return text("goal.later", days=shift) if shift > 0 else text("goal.same")


# ------------------------------------------------------------------------------- recovery


def _mean(vals: Iterable[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return statistics.fmean(v) if v else None


def _hrv_z7(inp: DigestInputs, day: dt.date) -> float | None:
    """The 7-day ln(rMSSD) mean vs baseline of ``day`` (readiness v2's HRV input, ADR-0013).

    Computed with the readiness model's own function rather than read from ``readiness_daily``:
    rows stored before v2 hold a single-day z, and the band must mean the same on every day.
    """
    r = readiness_job.inputs_for(inp.ds, day)
    hw = readiness_mod.hrv_window(day, r.today.hrv if r.today else None, r.history)
    return round(hw.z7, 2) if hw is not None else None


def _feel_gap(inp: DigestInputs, day: dt.date) -> float | None:
    rated = [r for r in inp.ds.on(day) if r.is_ride and (r.rpe is not None or r.feel is not None)]
    if not rated:
        return None
    longest = max(rated, key=lambda r: (r.moving_s, -r.id))
    return build_feel(inp.ride(longest), None, None).gap


def build_recovery(inp: DigestInputs, start: dt.date, end: dt.date) -> Recovery | None:
    """HRV band, resting HR, sleep, feel vs expected and readiness per day (``None``: no data)."""
    ds = inp.ds
    out: list[RecoveryDay] = []
    for d in days(start, end):
        w = ds.wellness.get(d)
        row = inp.session.get(ReadinessDaily, (ds.athlete_id, d))
        out.append(
            RecoveryDay(
                day=d,
                hrv_z7=_hrv_z7(inp, d),
                rhr=w.resting_hr if w is not None else None,
                sleep_h=round(w.sleep_s / 3600, 1) if w is not None and w.sleep_s else None,
                readiness=row.recommendation if row is not None else None,
                feel_gap=_feel_gap(inp, d),
            )
        )
    fields = ("hrv_z7", "rhr", "sleep_h", "readiness", "feel_gap")
    if not any(getattr(x, f) is not None for x in out for f in fields):
        return None
    rest_days = sum(1 for d in days(start, end) if not any(a.is_ride for a in ds.on(d)))
    counts = Counter(x.readiness for x in out if x.readiness is not None)
    readiness_counts = {k: counts[k] for k in RECOMMENDATIONS if counts[k]}
    swc = readiness_mod.HRV_SWC_SD
    base_lo = start - dt.timedelta(days=BASELINE_DAYS)
    base = [ds.wellness[d] for d in days(base_lo, start - dt.timedelta(days=1)) if d in ds.wellness]
    reasons: list[Reason] = []
    z7 = [x.hrv_z7 for x in out if x.hrv_z7 is not None]
    if z7:
        below = sum(1 for z in z7 if z < -swc)
        above = sum(1 for z in z7 if z > swc)
        reasons.append(
            Reason(
                text_zh=text(
                    "recovery.hrv",
                    n=len(z7),
                    below=below,
                    within=len(z7) - below - above,
                    above=above,
                    swc=swc,
                    low=min(z7),
                ),
                evidence={"days": len(z7), "below": below, "above": above, "swc_sd": swc},
            )
        )
    rhr, rhr_base = _mean(x.rhr for x in out), _mean(w.resting_hr for w in base)
    if rhr is not None:
        reasons.append(
            Reason(
                text_zh=(
                    text("recovery.rhr", mean=rhr, base=rhr_base, diff=rhr - rhr_base)
                    if rhr_base is not None
                    else text("recovery.rhr_no_base", mean=rhr)
                ),
                evidence={
                    "rhr_mean": round(rhr, 1),
                    "rhr_base": round(rhr_base, 1) if rhr_base is not None else None,
                    "baseline_days": BASELINE_DAYS,
                },
            )
        )
    sleep = _mean(x.sleep_h for x in out)
    sleep_base = _mean(w.sleep_s / 3600 if w.sleep_s else None for w in base)
    if sleep is not None:
        reasons.append(
            Reason(
                text_zh=(
                    text("recovery.sleep", mean=sleep, base=sleep_base)
                    if sleep_base is not None
                    else text("recovery.sleep_no_base", mean=sleep)
                ),
                evidence={
                    "sleep_h": round(sleep, 2),
                    "sleep_base_h": round(sleep_base, 2) if sleep_base is not None else None,
                },
            )
        )
    gaps = [x.feel_gap for x in out if x.feel_gap is not None]
    if gaps:
        harder = sum(1 for g in gaps if g >= readiness_mod.RPE_SURPRISE)
        reasons.append(
            Reason(
                text_zh=text(
                    "recovery.feel",
                    n=len(gaps),
                    gap=statistics.fmean(gaps),
                    harder=harder,
                    surprise=readiness_mod.RPE_SURPRISE,
                ),
                evidence={"rated_rides": len(gaps), "mean_gap": round(statistics.fmean(gaps), 2)},
            )
        )
    if readiness_counts:
        listed = text("common.list_sep").join(
            text("recovery.count", name=text(f"readiness.{k}"), n=n)
            for k, n in readiness_counts.items()
        )
        reasons.append(
            Reason(text_zh=text("recovery.readiness", counts=listed), evidence=readiness_counts)
        )
    reasons.append(
        Reason(text_zh=text("recovery.rest_days", n=rest_days), evidence={"rest_days": rest_days})
    )
    summary = join(r.text_zh for r in reasons)
    expl = Explanation(
        key=f"digest.recovery.{start.isoformat()}",
        headline_zh=reasons[0].text_zh,
        because=reasons,
        method=MethodRef(
            model_id=readiness_mod.ALGO_VERSION,
            version=readiness_mod.ALGO_VERSION,
            inputs={
                "hrv_window_days": readiness_mod.HRV_WINDOW_DAYS,
                "hrv_swc_sd": swc,
                "baseline_days": BASELINE_DAYS,
                "rpe_surprise": readiness_mod.RPE_SURPRISE,
            },
            doc="docs/glossary/readiness_v1.md",
            claim="readiness_v1.weights",
        ),
        confidence="high" if z7 and rhr is not None and sleep is not None else "low",
        glossary_terms=["readiness_v1"],
    )
    return Recovery(
        days=out,
        rest_days=rest_days,
        readiness_counts=readiness_counts,
        text_zh=summary,
        explanation=expl.to_json_dict(),
    )


# ------------------------------------------------------------------------------- next plan


def _season_lines(inp: DigestInputs, start: dt.date, end: dt.date) -> tuple[str, list[str]]:
    """(phase text, season markers: recovery / test weeks) of the weeks in ``[start, end]``."""
    sk = inp.skeleton
    weeks = [w for w in sk.weeks if w.end >= start and w.start <= end] if sk else []
    phases = []
    marks = []
    for w in weeks:
        tag = text("next.recovery_tag") if w.recovery else ""
        phases.append(text("next.week", start=w.start, n=w.index, phase=PHASE_ZH[w.phase]) + tag)
        if w.recovery:
            marks.append(text("next.recovery_week", start=w.start, n=w.index))
        if w.test is not None:
            marks.append(
                text("next.test_week", start=w.start, n=w.index, test=text(f"test.{w.test}"))
            )
    return text("common.list_sep").join(phases), marks


def build_next(inp: DigestInputs, start: dt.date, end: dt.date, *, monthly: bool) -> NextPlan:
    """The stored plan for ``[start, end]`` and the season weeks it falls in."""
    lib = load_library()
    rows = list(
        inp.session.scalars(
            select(PlannedWorkout)
            .where(
                PlannedWorkout.athlete_id == inp.ds.athlete_id,
                PlannedWorkout.date_local >= start,
                PlannedWorkout.date_local <= end,
                is_live(),
            )
            .order_by(PlannedWorkout.date_local, PlannedWorkout.slot)
        )
    )
    sessions = [
        PlannedSession(
            day=pw.date_local,
            name=pw.name,
            purpose_zh=t.purpose_zh if (t := lib.get(pw.template_id or "")) is not None else "",
            tss=pw.target_tss,
            minutes=round(pw.target_duration_s / 60) if pw.target_duration_s else None,
            outdoor=not pw.indoor,
        )
        for pw in rows
    ]
    ours_days = {pw.date_local for pw in rows}
    for d in days(start, end):
        if d in ours_days:
            continue
        for ev in day_plans(inp, d)[1]:
            sessions.append(PlannedSession(day=d, name=ev.name or "", tss=ev.icu_training_load))
    sessions.sort(key=lambda s: s.day)
    phase_zh, marks = _season_lines(inp, start, end)
    key = [
        text("next.key", day=pw.date_local, name=pw.name)
        for pw in rows
        if pw.intent not in CEILING_INTENTS
    ]
    if monthly:
        key = marks + key
    tss = sum(s.tss or 0.0 for s in sessions)
    if not sessions:
        summary = text("next.none_month" if monthly else "next.none")
    elif monthly:
        summary = text(
            "next.month",
            n=len(sessions),
            tss=tss,
            last=sessions[-1].day,
            hard=len(key) - len(marks),
        )
    else:
        summary = text("next.week_summary", n=len(sessions), tss=tss, hard=len(key))
    return NextPlan(sessions=sessions, phase_zh=phase_zh, key_sessions_zh=key, text_zh=summary)
