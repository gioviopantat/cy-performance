"""Build a :class:`~cyp.reports.ride_model.RideReport` for one ride (docs/specs/ride-report.md).

Reads persisted results only: the activity and its ``activity_metrics``, the planned workout of
that day, settings / wellness / fitness through :func:`~cyp.dataset.load_dataset`, and the
ride's 1 Hz streams. Longitudinal numbers (FTP status, limiters) come from the same pure
function the trends job runs (:func:`~cyp.analysis.longitudinal.run.compute_trends`), evaluated
as of the ride's day, so the report does not depend on when the job last ran.

The only verdicts computed here are the v1 intent check (:mod:`cyp.analysis.ride.intent`), the
outdoor context (:mod:`cyp.analysis.ride.context`) and the goal projection
(:mod:`cyp.analysis.longitudinal.goal`); every threshold is a named constant there, quoted with
its value in the section's explanation. Athlete-facing text comes from
``reports/i18n/ride_report.zh-TW.yaml``; numbers are formatted into it here, never by the
renderer (docs/07).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from cyp.analysis.longitudinal import climbs as climbs_mod
from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal import limiters as lim_mod
from cyp.analysis.longitudinal import tid as tid_mod
from cyp.analysis.longitudinal.durability import LATE_KJ
from cyp.analysis.longitudinal.ftp import FtpStatus, compute_ftp_status
from cyp.analysis.longitudinal.run import TrendsResult, compute_trends
from cyp.analysis.readiness import (
    RPE_SURPRISE,
    YesterdayRide,
    expected_rpe,
)
from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.ride.classify import CLASS_ZH
from cyp.analysis.ride.frames import RideFrame, load_frame
from cyp.analysis.run import activity_local_date
from cyp.core.errors import AnalysisError, NotFoundError
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.timeutil import now_utc, week_start
from cyp.dataset import ActivityRow, Dataset, load_dataset
from cyp.planning.templates import (
    Repeat,
    ResolvedWorkout,
    Step,
    Template,
    TemplateError,
    load_library,
    resolve,
)
from cyp.reports.facts import ride_fact
from cyp.reports.ride_model import (
    ClimbCompare,
    ClimbEffort,
    Feel,
    FtpPoint,
    Goal,
    Headline,
    Intent,
    LimiterLine,
    Limiters,
    MethodUsed,
    OutdoorContext,
    RideReport,
    RideSummary,
    Segment,
    Series,
    Tomorrow,
    Verdict,
)
from cyp.settings import AthleteConfig
from cyp.store.models import Activity, ActivityMetrics, Athlete, IcuEvent, PlannedWorkout
from cyp.store.streams import StreamStore

TEXT_PATH = Path(__file__).resolve().parent / "i18n" / "ride_report.zh-TW.yaml"

#: Downsampling step (s) of the chart series: ~700 points for a 2-hour ride.
SERIES_STEP_S = 10
#: Intents judged by a power ceiling (stay easy) rather than a band (reach the stimulus).
CEILING_INTENTS = frozenset({"recovery", "endurance"})
#: Coggan: FTP ≈ 95 % of a maximal 20-minute effort.
FTP_FROM_20MIN = 0.95
#: Past ascents shown per climb (most recent first, then sorted by date).
CLIMB_HISTORY_MAX = 10
#: kJ already spent before a climb that makes it a different effort (≈ 1 h of Z2 for a 70 kg
#: rider; durability work shows power at a given effort falls with prior work done).
CLIMB_FATIGUE_KJ_MARGIN = 300.0
#: Temperature difference (°C) from which heat is worth naming when comparing ascents.
CLIMB_HEAT_MARGIN_C = 5.0
#: |RPE − expected| from which feel is called harder / easier than expected (half of the
#: readiness rule's :data:`~cyp.analysis.readiness.RPE_SURPRISE`, which caps tomorrow at EASY).
FEEL_GAP_NOTABLE = 1.5
#: Power zones (our 7-zone TIZ) whose minutes train each limiter; ``durability`` is minutes
#: moving after :data:`~cyp.analysis.longitudinal.durability.LATE_KJ` kJ instead.
LIMITER_ZONES: dict[str, tuple[str, ...]] = {
    "vo2_ceiling": ("Z5",),
    "sustained_power": ("Z4",),
    "grey_zone": ("Z1", "Z2"),
    "aerobic_volume": ("Z2",),
}

#: Model id -> glossary / doc path (section 9).
METHOD_DOCS: dict[str, str] = {
    "coggan_np_if_tss": "docs/glossary/coggan_np_if_tss.md",
    intent_mod.INTENT_VERSION: "docs/specs/ride-report.md",
    goal_mod.GOAL_VERSION: "docs/glossary/ftp_target_season.md",
    "eftp": "docs/glossary/eftp.md",
    ctx_mod.CONTEXT_VERSION: "docs/specs/ride-report.md",
    "climb_repeats": "docs/04-analysis-engine.md",
    "readiness_v1.1": "docs/glossary/readiness_v1.md",
    "limiters": "docs/04-analysis-engine.md",
    "planner": "docs/glossary/workout_library.md",
}


@cache
def _texts() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(TEXT_PATH.read_text(encoding="utf-8"))
    return data


def text(key: str, **values: Any) -> str:
    """zh-TW template ``section.name`` formatted with ``values``."""
    section, name = key.split(".", 1)
    template: str = _texts()[section][name]
    return template.format(**values)


# ------------------------------------------------------------------------------- inputs


def _frame(store: StreamStore, a: Activity) -> RideFrame | None:
    try:
        return load_frame(store, a.id, trainer=bool(a.trainer))
    except (NotFoundError, AnalysisError):
        return None


def _is_indoor(a: Activity) -> bool:
    return bool(a.trainer) or a.sport_type == "VirtualRide"


def _row(ds: Dataset | None, activity_id: int) -> ActivityRow | None:
    if ds is None:
        return None
    return next((r for r in ds.activities if r.id == activity_id), None)


def _power(frame: RideFrame | None, row: ActivityRow | None) -> np.ndarray | None:
    """Measured power we may judge with (``None`` without a meter or when it is unreliable)."""
    if frame is None or frame.watts is None or not np.isfinite(frame.watts).any():
        return None
    if row is not None and row.power_unreliable is not None:
        return None
    return frame.watts


# ------------------------------------------------------------------------------- summary


def _summary(a: Activity, fact: Mapping[str, Any]) -> RideSummary:
    start = dt.datetime.fromisoformat(a.start_local or a.start_utc).replace(tzinfo=None)
    return RideSummary(
        activity_id=a.id,
        name=fact["name"],
        start_local=start,
        sport=a.sport_type,
        indoor=_is_indoor(a),
        moving_min=fact["moving_min"],
        distance_km=fact["distance_km"],
        elev_gain_m=fact["elev_gain_m"],
        np_w=fact["np_w"],
        if_=fact["if"],
        tss=round(fact["load"]) if fact["load"] is not None else None,
        tss_source=fact["load_source"],
    )


# ------------------------------------------------------------------------------- intent


def _planned(
    session: Session, a: Activity, day: dt.date
) -> tuple[PlannedWorkout | None, IcuEvent | None]:
    """Our planned workout this ride executed, else the athlete's own icu WORKOUT of the day.

    With several rides on one day only the longest one carries the plan.
    """
    pw = session.scalar(select(PlannedWorkout).where(PlannedWorkout.executed_activity_id == a.id))
    if pw is not None:
        return pw, None
    lo = (day - dt.timedelta(days=1)).isoformat()
    hi = (day + dt.timedelta(days=2)).isoformat()
    same_day = [
        x
        for x in session.scalars(
            select(Activity).where(
                Activity.is_ride.is_(True), Activity.start_utc >= lo, Activity.start_utc < hi
            )
        )
        if activity_local_date(x) == day
    ]
    if same_day and max(same_day, key=lambda x: (x.moving_s or 0, -x.id)).id != a.id:
        return None, None
    pw = session.scalar(
        select(PlannedWorkout)
        .where(
            PlannedWorkout.date_local == day,
            PlannedWorkout.status.not_in(("cancelled", "superseded")),
        )
        .order_by(PlannedWorkout.slot)
    )
    if pw is not None:
        return pw, None
    ev = session.scalar(
        select(IcuEvent)
        .where(
            IcuEvent.category == "WORKOUT",
            IcuEvent.start_date_local >= day.isoformat(),
            IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
        )
        .order_by(IcuEvent.id)
    )
    if ev is not None and not (ev.external_id or "").startswith("cyp:"):
        return None, ev
    return None, None


def _leaves(w: ResolvedWorkout) -> list[tuple[Step, int]]:
    out: list[tuple[Step, int]] = []
    for it in w.items:
        if isinstance(it, Repeat):
            out += [(s, it.count) for s in it.steps]
        else:
            out.append((it, 1))
    return out


def intent_target(
    w: ResolvedWorkout, intent: str, ftp: float
) -> tuple[intent_mod.IntentTarget, float, float] | None:
    """The planned stimulus in watts plus its band in % FTP (``None`` without a power band).

    Band intents use the ``work`` steps: band = the work band holding the most planned time
    (surges above it still count as on-target), reps from :func:`_rep_lengths`, rep length = their
    median; without work steps, or for :data:`CEILING_INTENTS`, the longest targeted step.
    Ceiling intents are judged over the whole planned duration.
    """
    leaves = [(s, c) for s, c in _leaves(w) if s.lo is not None and s.hi is not None]
    works = [(s, c) for s, c in leaves if s.kind == "work"]
    if intent in CEILING_INTENTS or not works:
        steady = [(s, c) for s, c in leaves if s.kind in ("steady", "work")]
        if not steady:
            return None
        main, count = max(steady, key=lambda sc: sc[0].duration_s * sc[1])
        assert main.lo is not None and main.hi is not None
        if intent in CEILING_INTENTS:
            target = intent_mod.IntentTarget(
                "ceiling", main.lo * ftp / 100, main.hi * ftp / 100, w.duration_s
            )
        else:
            target = intent_mod.IntentTarget(
                "band", main.lo * ftp / 100, main.hi * ftp / 100, main.duration_s * count
            )
        return target, main.lo, main.hi
    reps_s = _rep_lengths(w)
    by_band: dict[tuple[float, float], int] = {}
    for st, c in works:
        assert st.lo is not None and st.hi is not None
        by_band[(st.lo, st.hi)] = by_band.get((st.lo, st.hi), 0) + st.duration_s * c
    lo, hi = max(by_band, key=lambda b: by_band[b])
    rep_s = int(statistics.median(reps_s))
    target_s = sum(reps_s)
    return (
        intent_mod.IntentTarget(
            "band", lo * ftp / 100, hi * ftp / 100, target_s, rep_s, len(reps_s)
        ),
        lo,
        hi,
    )


def _rep_lengths(w: ResolvedWorkout) -> list[int]:
    """Planned rep lengths (s).

    A repeat made only of work steps (e.g. tempo with surges) is one continuous rep; otherwise
    each work step of each repetition is a rep.
    """
    out: list[int] = []
    for it in w.items:
        if isinstance(it, Repeat):
            works = [st.duration_s for st in it.steps if st.kind == "work"]
            if works and len(works) == len(it.steps):
                out.append(it.count * sum(works))
            else:
                out += works * it.count
        elif it.kind == "work":
            out.append(it.duration_s)
    return out


def _resolved(
    pw: PlannedWorkout, lib: Mapping[str, Template], outdoor: bool
) -> tuple[Template, ResolvedWorkout] | None:
    t = lib.get(pw.template_id or "")
    if t is None:
        return None
    params = (pw.steps or {}).get("params") if isinstance(pw.steps, dict) else None
    try:
        return t, resolve(t, dict(params or {}), outdoor=outdoor)
    except TemplateError:
        return None


def _segments(spans: Iterable[intent_mod.Span], label: str | None = None) -> list[Segment]:
    out = []
    for i, s in enumerate(spans, start=1):
        if label is not None:
            lab = text(label, n=i)
        else:
            lab = text("intent.excused_stop" if s.reason == "stop" else "intent.excused_coast")
        out.append(Segment(start_s=s.start_s, end_s=s.end_s, label_zh=lab))
    return out


def build_intent(
    session: Session,
    a: Activity,
    day: dt.date,
    *,
    fact: Mapping[str, Any],
    frame: RideFrame | None,
    row: ActivityRow | None,
    ftp: float | None,
) -> Intent | None:
    """Section 2 (``None`` when nothing was planned for this ride)."""
    pw, ev = _planned(session, a, day)
    if pw is None and ev is None:
        return None
    load_actual = float(fact["load"]) if fact["load"] is not None else None
    load_planned = pw.target_tss if pw is not None else ev.icu_training_load if ev else None
    ratio = (
        round(load_actual / load_planned, 3) if load_actual is not None and load_planned else None
    )
    resolved = _resolved(pw, load_library(), not _is_indoor(a)) if pw is not None else None
    intent_id = pw.intent if pw is not None else ""
    cls = CLASS_ZH.get(intent_id, intent_id or "")
    name = pw.name if pw is not None else (ev.name if ev is not None else "") or ""
    purpose = resolved[0].purpose_zh if resolved else ""
    power = _power(frame, row)
    reasons: list[Reason] = []
    inputs: dict[str, Any] = {
        "achieved_frac": intent_mod.ACHIEVED_FRAC,
        "partial_frac": intent_mod.PARTIAL_FRAC,
    }
    base = {
        "planned_name": name,
        "purpose_zh": purpose,
        "load_planned": round(load_planned, 1) if load_planned is not None else None,
        "load_actual": round(load_actual, 1) if load_actual is not None else None,
        "load_ratio": ratio,
    }
    tgt = intent_target(resolved[1], intent_id, ftp) if resolved and ftp else None
    if load_actual is not None and load_planned:
        reasons.append(
            Reason(
                text_zh=text("intent.load", actual=load_actual, planned=load_planned, ratio=ratio),
                evidence={"load_actual": load_actual, "load_planned": load_planned, "ratio": ratio},
            )
        )

    if tgt is None:
        # No power band (an icu workout of the athlete's own, or no FTP): judge the load only.
        verdict: Verdict = intent_mod.load_verdict(ratio)
        reasons.append(
            Reason(
                text_zh=text(
                    "intent.load_rule",
                    achieved=intent_mod.ACHIEVED_FRAC,
                    partial=intent_mod.PARTIAL_FRAC,
                ),
                evidence=inputs,
            )
        )
        headline = (
            text(
                "intent.headline_load",
                actual=load_actual,
                planned=load_planned,
                ratio=ratio,
                verdict=text(f"verdict.{verdict}"),
            )
            if ratio is not None
            else text("intent.headline_no_data")
        )
        return Intent(
            **base,
            target_zone=text("intent.zone_load", cls=cls or name),
            target_min=0.0,
            achieved_min=0.0,
            verdict=verdict,
            explanation=_intent_expl(headline, reasons, inputs, "low"),
        )

    target, lo_pct, hi_pct = tgt
    kind_ceiling = target.kind == "ceiling"
    zone = (
        text("intent.zone_ceiling", cls=cls, hi=hi_pct)
        if kind_ceiling
        else text("intent.zone", cls=cls, lo=lo_pct, hi=hi_pct)
    )
    common = {
        **base,
        "target_zone": zone,
        "target_low_w": round(target.lo_w),
        "target_high_w": round(target.hi_w),
        "target_min": round(target.target_s / 60, 1),
        "reps_planned": target.reps,
    }
    if power is None or frame is None:
        reasons.insert(0, Reason(text_zh=text("intent.no_power"), evidence={"has_power": False}))
        return Intent(
            **common,
            achieved_min=0.0,
            verdict="no_data",
            explanation=_intent_expl(text("intent.headline_no_data"), reasons, inputs, "low"),
        )

    facts = intent_mod.assess_intent(
        target, power, frame.moving, t_s=frame.t_s.astype(np.float64), grade=frame.grade
    )
    ceiling_w = target.hi_w * intent_mod.OVERSHOOT_FRAC
    window = intent_mod.SMOOTH_S
    if target.rep_s:
        window = max(
            3, min(intent_mod.SMOOTH_S, int(target.rep_s * intent_mod.SMOOTH_REP_FRACTION))
        )
    achieved_min = facts.achieved_s / 60
    target_min = target.target_s / 60
    verdict = facts.verdict
    inputs |= {
        "lo_w": round(target.lo_w, 1),
        "hi_w": round(target.hi_w, 1),
        "ftp": ftp,
        "overshoot_frac": intent_mod.OVERSHOOT_FRAC,
        "smooth_s": window,
        "target_s": target.target_s,
        "score": facts.score,
        "kind": target.kind,
        "above_s": facts.above_s,
        "over_share": round(facts.above_s / facts.moving_s, 3) if facts.moving_s else 0.0,
        "ceiling_w": round(ceiling_w, 1),
    }
    verdict_zh = text(f"verdict.{verdict}")
    if kind_ceiling:
        over = facts.above_s / facts.moving_s if facts.moving_s else 0.0
        headline = text(
            "intent.headline_ceiling",
            achieved=achieved_min,
            target=target_min,
            ceiling=ceiling_w,
            verdict=verdict_zh,
        )
        reasons[:0] = [
            Reason(
                text_zh=text(
                    "intent.ceiling_rule",
                    over=over,
                    ok=intent_mod.CEILING_OVER_OK,
                    max=intent_mod.CEILING_OVER_MAX,
                ),
                evidence={
                    "above_s": facts.above_s,
                    "moving_s": facts.moving_s,
                    "over_share": round(over, 3),
                    "ceiling_over_ok": intent_mod.CEILING_OVER_OK,
                    "ceiling_over_max": intent_mod.CEILING_OVER_MAX,
                },
            ),
            Reason(
                text_zh=text(
                    "intent.score_rule_ceiling",
                    score=facts.score or 0.0,
                    achieved=intent_mod.ACHIEVED_FRAC,
                    partial=intent_mod.PARTIAL_FRAC,
                ),
                evidence={"achieved_s": facts.achieved_s, "score": facts.score},
            ),
        ]
    else:
        if target.reps:
            headline = text(
                "intent.headline_reps",
                counted=facts.reps_counted,
                planned=target.reps,
                achieved=achieved_min,
                target=target_min,
                lo=target.lo_w,
                hi=target.hi_w,
                verdict=verdict_zh,
            )
        else:
            headline = text(
                "intent.headline_band",
                achieved=achieved_min,
                target=target_min,
                lo=target.lo_w,
                hi=target.hi_w,
                verdict=verdict_zh,
            )
        rule: list[Reason] = [
            Reason(
                text_zh=text(
                    "intent.score_rule_reps" if target.reps else "intent.score_rule",
                    score=facts.score or 0.0,
                    achieved=intent_mod.ACHIEVED_FRAC,
                    partial=intent_mod.PARTIAL_FRAC,
                ),
                evidence={
                    "achieved_s": facts.achieved_s,
                    "reps_counted": facts.reps_counted,
                    "reps_planned": target.reps,
                    "score": facts.score,
                },
            )
        ]
        if target.reps and target.rep_s:
            rule.append(
                Reason(
                    text_zh=text(
                        "intent.reps_rule",
                        lo=target.lo_w,
                        rep_min=target.rep_s / 60,
                        frac=intent_mod.REP_COUNT_FRAC,
                        dip=target.lo_w * intent_mod.DIP_FLOOR_FRAC,
                        dip_frac=intent_mod.DIP_FLOOR_FRAC,
                        max_s=intent_mod.EXCUSE_MAX_S,
                    ),
                    evidence={
                        "rep_s": target.rep_s,
                        "rep_count_frac": intent_mod.REP_COUNT_FRAC,
                        "dip_floor_frac": intent_mod.DIP_FLOOR_FRAC,
                    },
                )
            )
            rule.append(
                Reason(
                    text_zh=text(
                        "intent.excuse_rule",
                        max_s=intent_mod.EXCUSE_MAX_S,
                        grade=intent_mod.DESCENT_GRADE_PCT,
                        n=len(facts.excused),
                    ),
                    evidence={
                        "excuse_max_s": intent_mod.EXCUSE_MAX_S,
                        "excuse_share": intent_mod.EXCUSE_SHARE,
                        "descent_grade_pct": intent_mod.DESCENT_GRADE_PCT,
                        "n_excused": len(facts.excused),
                    },
                )
            )
        if facts.above_s >= 60:
            rule.append(
                Reason(
                    text_zh=text("intent.above", above=facts.above_s / 60, ceiling=ceiling_w),
                    evidence={"above_s": facts.above_s, "ceiling_w": round(ceiling_w, 1)},
                )
            )
        reasons[:0] = rule
    reasons.append(
        Reason(
            text_zh=text(
                "intent.band",
                lo=target.lo_w,
                hi=target.hi_w,
                ftp=ftp,
                lo_pct=lo_pct,
                hi_pct=hi_pct,
                overshoot=intent_mod.OVERSHOOT_FRAC - 1,
                ceiling=ceiling_w,
            ),
            evidence={"lo_pct": lo_pct, "hi_pct": hi_pct, "ftp": ftp},
        )
    )
    reasons.append(
        Reason(text_zh=text("intent.smooth", window=window), evidence={"smooth_s": window})
    )
    return Intent(
        **common,
        achieved_min=round(achieved_min, 1),
        reps_counted=facts.reps_counted,
        counted=_segments(facts.counted, "intent.counted"),
        excused=_segments(facts.excused),
        verdict=verdict,
        explanation=_intent_expl(headline, reasons, inputs, "medium"),
    )


def _intent_expl(
    headline: str, reasons: list[Reason], inputs: dict[str, Any], confidence: str
) -> dict[str, Any]:
    return Explanation(
        key="ride.intent",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id=intent_mod.INTENT_VERSION,
            version=intent_mod.INTENT_VERSION,
            inputs=inputs,
            doc=METHOD_DOCS[intent_mod.INTENT_VERSION],
        ),
        confidence=confidence,  # type: ignore[arg-type]
        glossary_terms=["workout_library", "coggan_np_if_tss"],
    ).to_json_dict()


# ------------------------------------------------------------------------------- goal


def _prev_ride_day(ds: Dataset, a_id: int, day: dt.date) -> dt.date | None:
    days = [r.date for r in ds.rides(None, day) if r.id != a_id and r.date <= day]
    return max(days) if days else None


def build_goal(
    ds: Dataset,
    cfg: AthleteConfig | None,
    a_id: int,
    day: dt.date,
    status: FtpStatus,
    row: ActivityRow | None,
) -> Goal | None:
    """Section 3 (``None`` without an FTP goal or a current FTP)."""
    g = next((g for g in cfg.goals if g.kind == "ftp_target"), None) if cfg else None
    target = g.target.get("ftp") if g else None
    if g is None or not target or not status.current_ftp:
        return None
    target_w = float(target)
    setting = float(status.current_ftp)
    proj = goal_mod.project_goal(status.estimates, as_of=day, target_w=target_w, setting_w=setting)
    prev_day = _prev_ride_day(ds, a_id, day)
    prev = None
    if prev_day is not None:
        prev_status = status if prev_day == day else compute_ftp_status(ds, prev_day)
        prev = goal_mod.project_goal(
            prev_status.estimates,
            as_of=prev_day,
            target_w=target_w,
            setting_w=float(prev_status.current_ftp or setting),
        )
    shift = (
        (proj.projected_date - prev.projected_date).days
        if prev is not None and prev.projected_date and proj.projected_date
        else None
    )
    evidence = None
    p20 = row.power_curve.get(1200) if row is not None and row.power_ok else None
    if p20 and p20 >= lim_mod.SUSTAINED_FTP_FRAC * status.current_ftp:
        evidence = round(p20 * FTP_FROM_20MIN, 1)
    settings_row = ds.settings_on(day)
    since = min(cfg.season.start if cfg else day, day - dt.timedelta(days=7 * goal_mod.TREND_WEEKS))
    history = [
        FtpPoint(day=s.effective_from, ftp_w=float(s.ftp), source=f"setting:{s.source}")
        for s in ds.settings
        if s.ftp and since <= s.effective_from <= day
    ] + [FtpPoint(day=d, ftp_w=round(v, 1), source=status.estimate_source) for d, v in proj.weekly]
    history.sort(key=lambda p: (p.day, p.source))
    on_track = proj.projected_date <= g.date if proj.projected_date else None

    reasons = [
        Reason(
            text_zh=text("goal.source", source=status.estimate_source),
            evidence={"estimate_source": status.estimate_source},
        )
    ]
    if proj.slope_w_per_week is not None and proj.level_w is not None:
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.trend",
                    weeks=len(proj.weekly),
                    slope=proj.slope_w_per_week,
                    tau=proj.kendall_tau or 0.0,
                    level=proj.level_w,
                ),
                evidence={
                    "weeks": len(proj.weekly),
                    "slope_w_per_week": proj.slope_w_per_week,
                    "kendall_tau": proj.kendall_tau,
                    "level_w": proj.level_w,
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
    rule_numbers = {
        "min_weeks": goal_mod.MIN_WEEKS,
        "min_slope": goal_mod.MIN_SLOPE_W_PER_WEEK,
        "min_tau": goal_mod.MIN_KENDALL_TAU,
        "max_days": goal_mod.MAX_PROJECTION_DAYS,
        "weeks": len(proj.weekly),
        "slope": proj.slope_w_per_week or 0.0,
        "tau": proj.kendall_tau or 0.0,
        "level": proj.level_w or 0.0,
        "target": target_w,
        "setting": setting,
        "below": goal_mod.MAX_BELOW_SETTING,
        "gap": 1 - (proj.level_w or 0.0) / setting,
    }
    if proj.reason is not None:
        reasons.append(Reason(text_zh=text(f"goal.{proj.reason}", **rule_numbers), evidence={}))
    elif proj.level_w is not None and proj.level_w >= target_w:
        reasons.append(Reason(text_zh=text("goal.reached", **rule_numbers), evidence={}))
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
    if evidence is not None and p20:
        reasons.append(
            Reason(
                text_zh=text(
                    "goal.evidence", p20=p20, frac=lim_mod.SUSTAINED_FTP_FRAC, ev=evidence
                ),
                evidence={"p20_w": round(p20, 1), "ftp_evidence_w": evidence},
            )
        )
    reasons.append(Reason(text_zh=text("goal.method", **rule_numbers), evidence={}))
    headline = (
        text(
            "goal.headline_date", weeks=len(proj.weekly), date=proj.projected_date, target=target_w
        )
        if proj.projected_date
        else text("goal.headline", current=status.current_ftp, target=target_w, target_date=g.date)
    )
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
                "min_slope_w_per_week": goal_mod.MIN_SLOPE_W_PER_WEEK,
                "min_kendall_tau": goal_mod.MIN_KENDALL_TAU,
                "max_below_setting": goal_mod.MAX_BELOW_SETTING,
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
        current_ftp_w=float(status.current_ftp),
        current_source=settings_row.source if settings_row else "unknown",
        this_ride_evidence_w=evidence,
        projected_date=proj.projected_date,
        projected_shift_days=shift,
        on_track=on_track,
        history=history,
        explanation=expl.to_json_dict(),
    )


# ------------------------------------------------------------------------------- outdoor


def build_outdoor(frame: RideFrame | None, indoor: bool) -> OutdoorContext | None:
    """Section 4 (``None`` indoors or without streams)."""
    if indoor or frame is None:
        return None
    f = ctx_mod.outdoor_context(frame)
    return OutdoorContext(
        stops_n=len(f.stops),
        stopped_min=round(f.stopped_s / 60, 1),
        coasting_pct=round(f.coasting_pct, 1) if f.coasting_pct is not None else None,
        temp_avg_c=round(f.temp_avg_c, 1) if f.temp_avg_c is not None else None,
        temp_max_c=round(f.temp_max_c, 1) if f.temp_max_c is not None else None,
        stops=[
            Segment(start_s=s.start_s, end_s=s.end_s, label_zh=text("outdoor.stop", sec=s.seconds))
            for s in f.stops
        ],
        caveats_zh=[text(f"outdoor.{c.code}", **c.evidence) for c in f.caveats],
    )


# ------------------------------------------------------------------------------- climbs


def _kj_and_temp(
    watts: np.ndarray | None, temp: np.ndarray | None, start_s: int, duration_s: int
) -> tuple[float | None, float | None]:
    kj = None
    if watts is not None and start_s <= watts.size:
        kj = round(float(np.nansum(np.nan_to_num(watts[:start_s], nan=0.0))) / 1000, 1)
    t = None
    if temp is not None:
        seg = temp[start_s : start_s + duration_s + 1]
        seg = seg[np.isfinite(seg)]
        t = round(float(seg.mean()), 1) if seg.size else None
    return kj, t


def _stream_cols(
    store: StreamStore, activity_id: int
) -> tuple[np.ndarray | None, np.ndarray | None]:
    try:
        df = store.read(activity_id)
    except NotFoundError:
        return None, None
    df = df.sort("t_s")

    def col(name: str) -> np.ndarray | None:
        if name not in df.columns:
            return None
        return np.asarray(df[name].cast(float).to_numpy(), dtype=np.float64)

    return col("watts"), col("temp_c")


def _climb_json(row: ActivityRow) -> list[dict[str, Any]]:
    cl = list(row.climbs)
    return climbs_mod.strip_power(cl) if row.power_unreliable is not None else cl


def build_climbs(
    ds: Dataset, store: StreamStore, row: ActivityRow | None, frame: RideFrame | None
) -> list[ClimbCompare]:
    """Section 5: each fingerprinted climb of this ride that has at least one past ascent."""
    if row is None or not row.climbs:
        return []
    rides = [r for r in ds.rides(None, row.date) if r.id == row.id or r.date <= row.date]
    by_id = {r.id: r for r in rides}
    boards = {
        b.fingerprint: b
        for b in climbs_mod.leaderboard((r.id, r.date, _climb_json(r)) for r in rides)
    }
    streams: dict[int, tuple[np.ndarray | None, np.ndarray | None]] = {}
    if frame is not None:
        streams[row.id] = (
            None if row.power_unreliable is not None else frame.watts,
            frame.col("temp_c"),
        )

    def effort(aid: int, day: dt.date, c: Mapping[str, Any]) -> ClimbEffort:
        if aid not in streams:
            w, t = _stream_cols(store, aid)
            streams[aid] = (None if by_id[aid].power_unreliable is not None else w, t)
        w, t = streams[aid]
        kj, temp = _kj_and_temp(w, t, int(c.get("start_s") or 0), int(c["duration_s"]))
        return ClimbEffort(
            day=day,
            power_w=c.get("avg_w"),
            w_kg=c.get("w_kg"),
            time_s=int(c["duration_s"]),
            kj_before=kj,
            temp_c=temp,
        )

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
        this = effort(row.id, row.date, c)
        history = []
        for e in sorted(past, key=lambda e: (e.date, e.activity_id)):
            src = next(
                (
                    x
                    for x in _climb_json(by_id[e.activity_id])
                    if x.get("fingerprint") == fp and x.get("duration_s") == e.duration_s
                ),
                None,
            )
            if src is not None:
                history.append(effort(e.activity_id, e.date, src))
        out.append(
            ClimbCompare(
                name=text(
                    "climb.name",
                    km=(c.get("distance_m") or 0) / 1000,
                    grade=c.get("avg_grade_pct") or 0.0,
                ),
                this=this,
                history=history,
                rank=rank,
                of_n=len(board.efforts),
                note_zh=_climb_note(this, history, rank, len(board.efforts)),
            )
        )
    return out


def _median(vals: Iterable[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return float(statistics.median(v)) if v else None


def _climb_note(this: ClimbEffort, history: list[ClimbEffort], rank: int | None, n: int) -> str:
    parts: list[str] = []
    if rank is not None:
        parts.append(text("climb.rank", rank=rank, of_n=n))
    t_med = _median(e.time_s for e in history)
    if t_med is not None:
        diff = round(this.time_s - t_med)
        if diff:
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
    if this.kj_before is not None and kj_med is not None:
        d = this.kj_before - kj_med
        if d >= CLIMB_FATIGUE_KJ_MARGIN:
            parts.append(
                text("climb.fatigue", kj=this.kj_before, diff=d, margin=CLIMB_FATIGUE_KJ_MARGIN)
            )
        elif -d >= CLIMB_FATIGUE_KJ_MARGIN:
            parts.append(
                text("climb.fresh", kj=this.kj_before, diff=-d, margin=CLIMB_FATIGUE_KJ_MARGIN)
            )
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
    return text("common.sep").join(parts)


# ------------------------------------------------------------------------------- feel


def build_feel(
    fact: Mapping[str, Any],
    a: Activity,
    feedback: tuple[float | None, int | None] | None,
    rate_url: str | None,
) -> Feel:
    """Section 6: RPE / feel against the expected RPE of this class and length."""
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
        key = (
            "feel.feel_strong"
            if feel <= 2
            else "feel.feel_normal"
            if feel == 3
            else "feel.feel_weak"
        )
        parts.append(text(key, feel=feel))
    return Feel(
        rated=True,
        rpe=float(rpe) if rpe is not None else None,
        feel=feel,
        expected_rpe=expected if rpe is not None else None,
        gap=gap,
        text_zh=text("common.sep").join(parts),
        rate_url=rate_url,
    )


# ------------------------------------------------------------------------------- limiters


def _late_minutes(watts: np.ndarray | None, moving: np.ndarray | None) -> float:
    """Moving minutes after the cumulative work passed :data:`LATE_KJ` kJ."""
    if watts is None:
        return 0.0
    kj = np.cumsum(np.nan_to_num(watts, nan=0.0)) / 1000
    late = kj >= LATE_KJ
    if moving is not None:
        late &= moving
    return float(np.count_nonzero(late)) / 60


def _zone_minutes(r: ActivityRow, zones: tuple[str, ...]) -> float:
    if not r.power_ok:
        return 0.0
    picked = tid_mod.tiz_from_metrics(r.tiz_power, None, r.icu_zone_times)
    if picked is None:
        return 0.0
    return sum(picked[0].get(z, 0.0) for z in zones) / 60


def build_limiters(
    trends: TrendsResult | None,
    ds: Dataset,
    store: StreamStore,
    row: ActivityRow | None,
    frame: RideFrame | None,
) -> Limiters | None:
    """Section 7: minutes this ride and this ISO week spent on each detected limiter."""
    if trends is None or row is None:
        return None
    detected = trends.report.limiters
    checks = trends.report.limiter_checks
    if not detected and checks:
        names = text("common.list_sep").join(
            _texts()["limiter_names"].get(str(c["id"]), str(c["id"])) for c in checks
        )
        return Limiters(lines=[], text_zh=text("limiters.insufficient", n=len(checks), names=names))
    if not detected:
        return Limiters(lines=[], text_zh=text("limiters.none"))
    week = [r for r in ds.rides(week_start(row.date), row.date)]
    late_cache: dict[int, float] = {}

    def late(r: ActivityRow) -> float:
        if r.id not in late_cache:
            if not r.power_ok or (r.kj or 0) < LATE_KJ:
                late_cache[r.id] = 0.0
            elif r.id == row.id and frame is not None:
                late_cache[r.id] = _late_minutes(frame.watts, frame.moving)
            else:
                w, _ = _stream_cols(store, r.id)
                late_cache[r.id] = _late_minutes(w, None)
        return late_cache[r.id]

    lines = []
    for lim in detected:
        lid = str(lim["id"])
        zones = LIMITER_ZONES.get(lid)
        if zones is None and lid != "durability":
            continue

        def minutes(r: ActivityRow, lid: str = lid, zones: tuple[str, ...] | None = zones) -> float:
            return late(r) if lid == "durability" else _zone_minutes(r, zones or ())

        lines.append(
            LimiterLine(
                limiter_id=lid,
                name_zh=str(lim["title_zh"]),
                trained_min=round(minutes(row), 1),
                week_min=round(sum(minutes(r) for r in week), 1),
            )
        )
    if not lines:
        return Limiters(lines=[], text_zh=text("limiters.none"))
    top = lines[0]
    key = "limiters.trained" if top.trained_min >= 1 else "limiters.not_trained"
    return Limiters(
        lines=lines,
        text_zh=text(key, name=top.name_zh, minutes=top.trained_min, week=top.week_min),
    )


# ------------------------------------------------------------------------------- series


def build_series(frame: RideFrame | None, step_s: int = SERIES_STEP_S) -> Series | None:
    """Block means every ``step_s`` seconds (``None`` for a block without data)."""
    if frame is None:
        return None

    def down(x: np.ndarray | None) -> list[float | None]:
        if x is None:
            return []
        n = x.size // step_s * step_s
        out: list[float | None] = []
        blocks = x[:n].reshape(-1, step_s) if n else np.empty((0, step_s))
        for b in blocks:
            ok = b[np.isfinite(b)]
            out.append(round(float(ok.mean()), 1) if ok.size else None)
        rest = x[n:]
        if rest.size:
            ok = rest[np.isfinite(rest)]
            out.append(round(float(ok.mean()), 1) if ok.size else None)
        return out

    length = -(-frame.n // step_s)
    pad: list[float | None] = [None] * length

    def col(x: np.ndarray | None) -> list[float | None]:
        return down(x) if x is not None else []

    s = Series(
        step_s=step_s,
        power_w=col(frame.watts),
        hr_bpm=col(frame.hr),
        altitude_m=col(frame.alt),
        temp_c=col(frame.col("temp_c")),
    )
    # Equal-length lists: a missing stream is all None.
    for name in ("power_w", "hr_bpm", "altitude_m", "temp_c"):
        if not getattr(s, name):
            setattr(s, name, list(pad))
    return s


# ------------------------------------------------------------------------------- headline


def build_headline(
    intent: Intent | None,
    goal: Goal | None,
    tomorrow: Tomorrow | None,
    fact: Mapping[str, Any],
) -> Headline:
    """Section 1: deterministic sentence from the section verdicts (no LLM)."""
    parts: list[str] = []
    if intent is None:
        verdict: Verdict = "unplanned"
        parts.append(
            text("headline.unplanned", cls=fact["classification_zh"] or fact["sport"] or "")
        )
    else:
        verdict = intent.verdict
        parts.append(text(f"headline.{verdict}", name=intent.planned_name))
        if verdict != "no_data" and intent.target_min > 0:
            if intent.reps_planned and intent.reps_counted is not None:
                parts.append(
                    text("headline.reps", counted=intent.reps_counted, planned=intent.reps_planned)
                )
            inputs = ((intent.explanation or {}).get("method") or {}).get("inputs", {})
            if inputs.get("kind") == "ceiling":
                parts.append(
                    text(
                        "headline.ceiling",
                        over=inputs.get("over_share", 0.0),
                        ceiling=inputs.get("ceiling_w", 0.0),
                    )
                )
            else:
                parts.append(
                    text("headline.minutes", achieved=intent.achieved_min, target=intent.target_min)
                )
    if goal is not None:
        if goal.projected_date is not None:
            g = text("headline.goal_date", target=goal.target_w, date=goal.projected_date)
            if goal.projected_shift_days is not None:
                d = goal.projected_shift_days
                g += (
                    text("headline.goal_shift_earlier", days=-d)
                    if d < 0
                    else text("headline.goal_shift_later", days=d)
                    if d > 0
                    else text("headline.goal_shift_same")
                )
            parts.append(g + "。")
        else:
            parts.append(text("headline.goal_none", target=goal.target_w) + "。")
    if tomorrow is not None:
        if tomorrow.after_name is None:
            parts.append(text("headline.tomorrow_rest"))
        else:
            key = "headline.tomorrow_changed" if tomorrow.changed else "headline.tomorrow_same"
            parts.append(text(key, name=tomorrow.after_name))
    return Headline(text_zh="".join(parts), verdict=verdict)


# ------------------------------------------------------------------------------- methods


def build_methods(report: RideReport, fact: Mapping[str, Any]) -> list[MethodUsed]:
    """Section 9: one entry per model a number above came from (grade/claim filled later)."""
    ids: list[str] = []
    if fact["np_w"] is not None or fact["tss"] is not None:
        ids.append("coggan_np_if_tss")
    if report.intent is not None:
        ids.append(intent_mod.INTENT_VERSION)
    if report.goal is not None:
        ids += [goal_mod.GOAL_VERSION, "eftp"]
    if report.outdoor is not None:
        ids.append(ctx_mod.CONTEXT_VERSION)
    if report.climbs:
        ids.append("climb_repeats")
    if report.feel.rated:
        ids.append("readiness_v1.1")
    if report.limiters is not None:
        ids.append("limiters")
    if report.tomorrow is not None:
        ids.append("planner")
    return [MethodUsed(model_id=i, doc=METHOD_DOCS[i]) for i in ids]


# ------------------------------------------------------------------------------- entry


def build_ride_report(
    session: Session,
    store: StreamStore,
    activity_id: int,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    tomorrow: Tomorrow | None = None,
    rate_url: str | None = None,
    feedback: tuple[float | None, int | None] | None = None,
) -> RideReport:
    """Read persisted results and the ride's streams into a report.

    Computes no new verdicts beyond the v1 intent and outdoor-context rules in the spec.
    ``feedback`` is the web post-ride answer ``(rpe, feel)``; per field it wins over intervals.icu's
    (as in ``services.ride_feedback``).

    Raises:
        NotFoundError: unknown activity.
    """
    a = session.get(Activity, activity_id)
    if a is None:
        raise NotFoundError(f"no activity {activity_id}")
    m = session.get(ActivityMetrics, activity_id)
    fact = ride_fact(a, m)
    day = activity_local_date(a)
    ds = load_dataset(session)
    if ds is not None and cfg is not None:
        ds = ds.with_data_quality(cfg.data_quality.resolved())
    row = _row(ds, a.id)
    frame = _frame(store, a)
    indoor = _is_indoor(a)
    trends = compute_trends(ds, day) if ds is not None and row is not None else None
    ftp = ds.ftp_on(day) if ds is not None else None

    intent = build_intent(session, a, day, fact=fact, frame=frame, row=row, ftp=ftp)
    goal = (
        build_goal(ds, cfg, a.id, day, trends.ftp, row)
        if ds is not None and trends is not None
        else None
    )
    athlete = session.get(Athlete, a.athlete_id) if a.athlete_id is not None else None
    report = RideReport(
        profile=profile,
        athlete_name=(athlete.name if athlete and athlete.name else profile),
        generated_at=now_utc(),
        ride=_summary(a, fact),
        headline=build_headline(intent, goal, tomorrow, fact),
        intent=intent,
        goal=goal,
        outdoor=build_outdoor(frame, indoor),
        climbs=build_climbs(ds, store, row, frame) if ds is not None else [],
        feel=build_feel(fact, a, feedback, rate_url),
        limiters=build_limiters(trends, ds, store, row, frame) if ds is not None else None,
        tomorrow=tomorrow,
        series=build_series(frame),
    )
    report.methods = build_methods(report, fact)
    return report
