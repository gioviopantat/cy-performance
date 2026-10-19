"""Section 2 of the per-ride report: planned stimulus vs achieved (v1 intent check).

The planned workout is resolved from its template (``ResolvedWorkout.stimulus``) at the ride's
venue and turned into watts with the FTP of the day; :mod:`cyp.analysis.ride.intent` judges it.
Minutes are rounded to whole minutes once, here, and every text quotes the stored value.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import numpy as np
from sqlalchemy import select

from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.ride.classify import CLASS_ZH
from cyp.analysis.run import activity_local_date
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.adapt import OVERDONE_FACTOR, OVERDONE_MARGIN_TSS, is_overdone
from cyp.planning.templates import ResolvedWorkout, Template, TemplateError, load_library, resolve
from cyp.reports.ride_common import METHOD_DOCS, RideInputs, text
from cyp.reports.ride_model import Intent, Segment, Verdict
from cyp.store.models import Activity, IcuEvent, PlannedWorkout
from cyp.store.repo.events import CYP_EXTERNAL_ID_PREFIX
from cyp.store.repo.planned import PlannedWorkoutRepo


def planned_for(inp: RideInputs) -> tuple[PlannedWorkout | None, IcuEvent | None]:
    """Our planned workout this ride executed, else the athlete's own icu WORKOUT of the day.

    A workout of ours the athlete dragged to this day in icu (its event is on this day, its
    external_id names another) counts as this day's plan. With several rides on one day only
    the longest one carries the plan.
    """
    s, a, day = inp.session, inp.activity, inp.day
    repo = PlannedWorkoutRepo(s)
    pw = repo.executed_by(a.id)
    if pw is not None:
        return pw, None
    same_day = [
        x
        for x in s.scalars(
            select(Activity).where(
                Activity.is_ride.is_(True),
                Activity.start_utc >= _iso(day - dt.timedelta(days=1)),
                Activity.start_utc < _iso(day + dt.timedelta(days=2)),
            )
        )
        if activity_local_date(x) == day
    ]
    if same_day and max(same_day, key=lambda x: (x.moving_s or 0, -x.id)).id != a.id:
        return None, None
    pw = repo.first_live_on(day)
    if pw is not None and repo.moved_day(pw) is None:
        return pw, None
    moved = repo.moved_onto(day)
    if moved:
        return moved[0], None
    ev = s.scalar(
        select(IcuEvent)
        .where(
            IcuEvent.category == "WORKOUT",
            IcuEvent.start_date_local >= day.isoformat(),
            IcuEvent.start_date_local < _iso(day + dt.timedelta(days=1)),
        )
        .order_by(IcuEvent.id)
    )
    if ev is not None and not (ev.external_id or "").startswith(CYP_EXTERNAL_ID_PREFIX):
        return None, ev
    return None, None


def _iso(day: dt.date) -> str:
    return day.isoformat()


def _resolved(pw: PlannedWorkout, outdoor: bool) -> tuple[Template, ResolvedWorkout] | None:
    t = load_library().get(pw.template_id or "")
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


def _expl(
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


def _verdict_rules() -> dict[str, float]:
    return {"achieved": intent_mod.ACHIEVED_FRAC, "partial": intent_mod.PARTIAL_FRAC}


def build_intent(
    inp: RideInputs, planned: tuple[PlannedWorkout | None, IcuEvent | None] | None = None
) -> Intent | None:
    """Section 2 (``None`` when nothing was planned for this ride).

    ``planned`` replaces the stored plan lookup (:func:`planned_for`); ``cyp plan replay``
    judges rides against its replayed plan this way. A ride the athlete tagged casual keeps
    its figures but gets the neutral verdict ``casual`` (:func:`as_casual`).
    """
    intent = _build_intent(inp, planned)
    return as_casual(intent) if intent is not None and inp.casual else intent


def as_casual(intent: Intent) -> Intent:
    """The same figures, not graded: verdict ``casual``, a neutral headline, the tag as reason."""
    expl = dict(intent.explanation or {})
    reason = Reason(
        text_zh=text("intent.casual_rule", name=intent.planned_name),
        evidence={"casual": True, "graded_verdict": intent.verdict},
    ).model_dump(mode="json")
    expl["headline_zh"] = text(
        "intent.headline_casual",
        name=intent.planned_name,
        load=intent.load_actual if intent.load_actual is not None else 0.0,
    )
    expl["because"] = [reason, *list(expl.get("because") or [])]
    return intent.model_copy(update={"verdict": "casual", "explanation": expl})


def _build_intent(
    inp: RideInputs, planned: tuple[PlannedWorkout | None, IcuEvent | None] | None
) -> Intent | None:
    pw, ev = planned if planned is not None else planned_for(inp)
    if pw is None and ev is None:
        return None
    fact = inp.fact
    load_actual = float(fact["load"]) if fact["load"] is not None else None
    load_planned = pw.target_tss if pw is not None else ev.icu_training_load if ev else None
    ratio = (
        round(load_actual / load_planned, 3) if load_actual is not None and load_planned else None
    )
    resolved = _resolved(pw, not inp.indoor) if pw is not None else None
    intent_id = pw.intent if pw is not None else ""
    cls = CLASS_ZH.get(intent_id, intent_id or "")
    name = (pw.name if pw is not None else ev.name if ev is not None else "") or ""
    ftp = inp.ftp
    stim = resolved[1].stimulus(intent_id) if resolved else None
    reasons: list[Reason] = []
    inputs: dict[str, Any] = {
        "achieved_frac": intent_mod.ACHIEVED_FRAC,
        "partial_frac": intent_mod.PARTIAL_FRAC,
    }
    base: dict[str, Any] = {
        "planned_name": name,
        "purpose_zh": resolved[0].purpose_zh if resolved else "",
        "load_planned": round(load_planned, 1) if load_planned is not None else None,
        "load_actual": round(load_actual, 1) if load_actual is not None else None,
        "load_ratio": ratio,
    }
    if load_actual is not None and load_planned:
        reasons.append(
            Reason(
                text_zh=text("intent.load", actual=load_actual, planned=load_planned, ratio=ratio),
                evidence={"load_actual": load_actual, "load_planned": load_planned, "ratio": ratio},
            )
        )

    if stim is None or not ftp:
        # No power band (an icu workout of the athlete's own, or no FTP): judge the load only.
        verdict: Verdict = intent_mod.load_verdict(ratio)
        reasons.append(
            Reason(text_zh=text("intent.load_rule", **_verdict_rules()), evidence=inputs)
        )
        if ratio is not None:
            headline = text(
                "intent.headline_load", actual=load_actual, planned=load_planned, ratio=ratio
            )
        else:
            headline = text("intent.headline_no_load")
        return Intent(
            **base,
            kind="work",
            target_zone=text("intent.zone_load", cls=cls or name),
            target_min=0.0,
            achieved_min=0.0,
            verdict=verdict,
            explanation=_expl(headline, reasons, inputs, "low"),
        )

    target = intent_mod.IntentTarget(
        stim.kind,
        stim.lo_pct * ftp / 100,
        stim.hi_pct * ftp / 100,
        stim.target_s,
        stim.rep_s,
        stim.reps,
        stim.floor_pct * ftp / 100 if stim.floor_pct is not None else None,
    )
    ceiling = target.kind == "ceiling"
    zone = (
        text("intent.zone_ceiling", cls=cls, hi=stim.hi_pct)
        if ceiling
        else text("intent.zone", cls=cls, lo=stim.lo_pct, hi=stim.hi_pct)
    )
    target_min = float(round(target.target_s / 60))
    common: dict[str, Any] = {
        **base,
        "kind": target.kind,
        "target_zone": zone,
        "target_low_w": round(target.lo_w),
        "target_high_w": round(target.hi_w),
        "target_min": target_min,
        "reps_planned": target.reps,
    }
    power, frame = inp.power, inp.frame
    if power is None or frame is None:
        reasons.insert(0, Reason(text_zh=text("intent.no_power"), evidence={"has_power": False}))
        return Intent(
            **common,
            achieved_min=0.0,
            verdict="no_data",
            explanation=_expl(text("intent.headline_no_power"), reasons, inputs, "low"),
        )

    np_w = float(fact["np_w"]) if fact["np_w"] is not None else None
    facts = intent_mod.assess_intent(
        target, power, frame.moving, t_s=frame.t_s.astype(np.float64), grade=frame.grade, np_w=np_w
    )
    ceiling_w = target.hi_w * intent_mod.OVERSHOOT_FRAC
    window = intent_mod.smoothing_window(target)
    achieved_min = float(round(facts.achieved_s / 60))
    over = facts.above_s / facts.moving_s if facts.moving_s else 0.0
    inputs |= {
        "kind": target.kind,
        "lo_w": round(target.lo_w, 1),
        "hi_w": round(target.hi_w, 1),
        "floor_w": round(target.floor_w, 1) if target.floor_w is not None else None,
        "ftp": ftp,
        "overshoot_frac": intent_mod.OVERSHOOT_FRAC,
        "smooth_s": window,
        "target_s": target.target_s,
        "score": facts.score,
        "above_s": facts.above_s,
        "over_share": round(over, 3),
        "ceiling_w": round(ceiling_w, 1),
    }
    rules = _verdict_rules()
    if ceiling:
        headline = text("intent.headline_ceiling", over=over, ceiling=ceiling_w, smooth=window)
        if facts.too_easy and target.floor_w is not None and np_w is not None:
            headline += text("intent.headline_too_easy", np=np_w, floor=target.floor_w)
        rule = [
            Reason(
                text_zh=text(
                    "intent.ceiling_rule",
                    over=over,
                    ok=intent_mod.CEILING_OVER_OK,
                    max=intent_mod.CEILING_OVER_MAX,
                    smooth=window,
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
                text_zh=text("intent.score_rule_ceiling", score=facts.score or 0.0, **rules),
                evidence={"achieved_s": facts.achieved_s, "score": facts.score},
            ),
        ]
        if target.floor_w is not None and np_w is not None:
            rule.append(
                Reason(
                    text_zh=text("intent.floor_rule", np=np_w, floor=target.floor_w),
                    evidence={
                        "np_w": np_w,
                        "floor_w": round(target.floor_w, 1),
                        "too_easy": facts.too_easy,
                    },
                )
            )
    else:
        nums = {
            "achieved": achieved_min,
            "target": target_min,
            "lo": target.lo_w,
            "hi": target.hi_w,
        }
        if target.reps:
            headline = text(
                "intent.headline_reps", counted=facts.reps_counted, planned=target.reps, **nums
            )
        else:
            headline = text("intent.headline_band", **nums)
        rule = [
            Reason(
                text_zh=text(
                    "intent.score_rule_reps" if target.reps else "intent.score_rule",
                    score=facts.score or 0.0,
                    **rules,
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
            rule += [
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
                ),
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
                ),
            ]
        if facts.above_s >= 60:
            rule.append(
                Reason(
                    text_zh=text(
                        "intent.above", above=round(facts.above_s / 60), ceiling=ceiling_w
                    ),
                    evidence={"above_s": facts.above_s, "ceiling_w": round(ceiling_w, 1)},
                )
            )
    verdict = facts.verdict
    if ceiling and ratio is not None and is_overdone(load_actual, load_planned):
        # An easy day ridden far past its planned load did not do its job, however easy each
        # minute was; the planner turns the next day into recovery on the same threshold.
        verdict = "missed"
        headline = text("intent.headline_overdone", ratio=ratio) + headline
        rule.insert(
            0,
            Reason(
                text_zh=text(
                    "intent.overdone_rule",
                    ratio=ratio,
                    max=OVERDONE_FACTOR,
                    margin=OVERDONE_MARGIN_TSS,
                ),
                evidence={
                    "load_ratio": ratio,
                    "overdone_factor": OVERDONE_FACTOR,
                    "overdone_margin_tss": OVERDONE_MARGIN_TSS,
                },
            ),
        )
        inputs["overdone_factor"] = OVERDONE_FACTOR
    reasons[:0] = rule
    reasons.append(
        Reason(
            text_zh=text(
                "intent.band",
                lo=target.lo_w,
                hi=target.hi_w,
                ftp=ftp,
                lo_pct=stim.lo_pct,
                hi_pct=stim.hi_pct,
                overshoot=intent_mod.OVERSHOOT_FRAC - 1,
                ceiling=ceiling_w,
            ),
            evidence={"lo_pct": stim.lo_pct, "hi_pct": stim.hi_pct, "ftp": ftp},
        )
    )
    reasons.append(
        Reason(text_zh=text("intent.smooth", window=window), evidence={"smooth_s": window})
    )
    return Intent(
        **common,
        achieved_min=achieved_min,
        reps_counted=facts.reps_counted,
        counted=_segments(facts.counted, "intent.counted"),
        excused=_segments(facts.excused),
        verdict=verdict,
        explanation=_expl(headline, reasons, inputs, "medium"),
    )
