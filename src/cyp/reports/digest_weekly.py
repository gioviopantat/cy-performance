"""The weekly digest (``digest_weekly_v1``): highlights, the headline and the assembly.

Sections 2-6 and 8 come from ``digest_sections`` / ``digest_progress``; section 7 (highlights)
and section 1 (the deterministic headline) live here. A week without a ride is a break: the
short version keeps the period, the planned (skipped) sessions, the load / CTL drift and the
next week's plan; every other section is ``None``.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence

from cyp.analysis import readiness as readiness_mod
from cyp.analysis.longitudinal import durability as dur_mod
from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride import intent as intent_mod
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.adapt import OVERDONE_FACTOR
from cyp.planning.gate import GATE_VERSION, GateDecision
from cyp.reports.digest_common import DigestInputs, join, method, text
from cyp.reports.digest_model import (
    DigestHeadline,
    Distribution,
    GoalProgress,
    Highlights,
    Load,
    NextPlan,
    PlanReason,
    Recovery,
    SessionLine,
    WeeklyDigest,
)
from cyp.reports.digest_progress import build_goal, build_next, build_recovery
from cyp.reports.digest_sections import (
    build_distribution,
    build_load,
    build_period,
    build_sessions,
    week_label,
)
from cyp.reports.facts import weekly_facts
from cyp.reports.ride_model import ClimbCompare, MethodUsed
from cyp.reports.ride_sections import build_climbs

#: Climbs shown in the highlights: the best ranked against their own history.
HIGHLIGHT_CLIMBS_MAX = 3
#: Days before the week whose median durability ratio is the athlete's recent norm.
DURABILITY_BASE_DAYS = 28
#: Durability ratio change (fraction) worth naming as better / worse than the norm.
DURABILITY_MARGIN = 0.02
#: Headline: credited share of judged sessions (achieved = 1, partial = PARTIAL_CREDIT) for
#: ``on_track`` / ``partly``; below that ``off_track``.
ON_TRACK_SHARE = 0.8
PARTLY_SHARE = 0.5
PARTIAL_CREDIT = 0.5
#: Skipped sessions that become the week's one change.
SKIPPED_ALERT = 2
#: Below this share of the planned load the volume is the one change.
LOAD_SHORT_RATIO = 0.8
#: Days with the 7-day HRV under its band that make recovery the one change.
HRV_LOW_DAYS_ALERT = 3


# ------------------------------------------------------------------------------- highlights


def _durability(
    inp: DigestInputs, start: dt.date, end: dt.date
) -> tuple[float | None, float | None, int]:
    lo = start - dt.timedelta(days=DURABILITY_BASE_DAYS)
    rides = [
        r
        for r in (
            dur_mod.from_json(a.id, a.date, a.durability)
            for a in inp.ds.rides(lo, end)
            if a.power_unreliable is None
        )
        if r is not None
    ]
    week = dur_mod.durability_trend(rides, end=end, n_blocks=1, block_days=7)[0]
    base = dur_mod.durability_trend(
        rides, end=start - dt.timedelta(days=1), n_blocks=1, block_days=DURABILITY_BASE_DAYS
    )[0]
    return week.median_ratio, base.median_ratio, week.n_long


def build_highlights(inp: DigestInputs, start: dt.date, end: dt.date) -> Highlights | None:
    """Section 7: best climbs vs their history and durability vs the 4-week median."""
    climbs: list[ClimbCompare] = []
    for row in inp.ds.rides(start, end):
        climbs += build_climbs(inp.ride(row))
    climbs.sort(key=lambda c: ((c.rank or 99) / (c.of_n or 1), -(c.of_n or 0), c.this.day))
    climbs = climbs[:HIGHLIGHT_CLIMBS_MAX]
    ratio, base, n_long = _durability(inp, start, end)
    if not climbs and ratio is None:
        return None
    parts = []
    if climbs:
        c = climbs[0]
        parts.append(text("highlights.climb", name=c.name, day=c.this.day, note=c.note_zh))
    reasons = []
    if ratio is not None:
        nums = {"ratio": ratio, "fade": 1 - ratio, "base": base, "n": n_long}
        if base is None:
            key = "highlights.durability_no_base"
        elif ratio - base >= DURABILITY_MARGIN:
            key = "highlights.durability_better"
        elif base - ratio >= DURABILITY_MARGIN:
            key = "highlights.durability_worse"
        else:
            key = "highlights.durability_same"
        parts.append(text(key, **nums, margin=DURABILITY_MARGIN, days=DURABILITY_BASE_DAYS))
        reasons.append(
            Reason(
                text_zh=parts[-1],
                evidence={
                    "median_ratio": ratio,
                    "base_median_ratio": base,
                    "n_long": n_long,
                    "late_kj": dur_mod.LATE_KJ,
                    "margin": DURABILITY_MARGIN,
                },
            )
        )
        reasons.append(Reason(text_zh=text("highlights.durability_method", kj=dur_mod.LATE_KJ)))
    expl = (
        Explanation(
            key=f"digest.durability.{start.isoformat()}",
            headline_zh=reasons[0].text_zh,
            because=reasons,
            method=MethodRef(
                model_id=dur_mod.DURABILITY_VERSION,
                version=dur_mod.DURABILITY_VERSION,
                inputs={
                    "late_kj": dur_mod.LATE_KJ,
                    "base_days": DURABILITY_BASE_DAYS,
                    "margin": DURABILITY_MARGIN,
                },
                doc="docs/glossary/decoupling_hr_lag.md",
                claim="decoupling_hr_lag.durability_kj_buckets",
            ),
            confidence="medium" if n_long >= 2 else "low",
            glossary_terms=["decoupling_hr_lag"],
        ).to_json_dict()
        if reasons
        else None
    )
    return Highlights(
        climbs=climbs,
        durability_ratio=ratio,
        durability_median_ratio=base,
        text_zh=join(parts),
        explanation=expl,
    )


# ------------------------------------------------------------------------------- headline


def tally(sessions: Sequence[SessionLine]) -> Counter[str]:
    """Count of each session verdict."""
    return Counter(s.verdict for s in sessions)


def plan_share(counts: Counter[str]) -> tuple[int, float | None]:
    """(sessions judged, credited share) over planned sessions with a verdict."""
    judged = sum(counts[v] for v in ("achieved", "partial", "missed", "overdone", "skipped"))
    if not judged:
        return 0, None
    return judged, (counts["achieved"] + PARTIAL_CREDIT * counts["partial"]) / judged


def break_text(reason: str, skipped: int) -> str:
    """The break headline: why, and how many planned sessions were not ridden."""
    out = text("headline.break", reason=reason)
    return out + text("headline.break_skipped", n=skipped) if skipped else out


def goal_clause(goal: GoalProgress | None) -> str:
    """The goal's one clause (projected date and its shift), shared with the monthly digest."""
    if goal is None:
        return ""
    if goal.projected_date is None:
        return text("headline.goal_none", target=goal.target_w)
    clause = text("headline.goal_date", target=goal.target_w, date=goal.projected_date)
    if goal.projected_date_before is not None:
        shift = (goal.projected_date - goal.projected_date_before).days
        clause += (
            text("headline.goal_earlier", days=-shift)
            if shift < 0
            else text("headline.goal_later", days=shift)
            if shift > 0
            else text("headline.goal_same")
        )
    return clause


def _change(
    counts: Counter[str],
    load: Load | None,
    dist: Distribution | None,
    recovery: Recovery | None,
) -> str:
    if counts["overdone"]:
        return text("change.overdone", n=counts["overdone"], max=OVERDONE_FACTOR)
    if counts["skipped"] >= SKIPPED_ALERT:
        return text("change.skipped", n=counts["skipped"])
    if dist is not None and dist.verdict == "over":
        hard = dist.sessions["mid"] + dist.sessions["high"]
        return text("change.too_hard", hard=hard, n=sum(dist.sessions.values()))
    if counts["missed"]:
        return text("change.missed", n=counts["missed"], frac=intent_mod.PARTIAL_FRAC)
    if recovery is not None:
        rest = recovery.readiness_counts.get("REST", 0)
        low = sum(
            1
            for d in recovery.days
            if d.hrv_z7 is not None and d.hrv_z7 < -readiness_mod.HRV_SWC_SD
        )
        if rest >= SKIPPED_ALERT or low >= HRV_LOW_DAYS_ALERT:
            return text("change.recovery", rest=rest, low=low)
    if (
        load is not None
        and load.planned_tss
        and load.actual_tss < LOAD_SHORT_RATIO * load.planned_tss
    ):
        return text("change.volume", ratio=load.actual_tss / load.planned_tss, min=LOAD_SHORT_RATIO)
    if dist is not None and dist.verdict == "under":
        return text("change.too_easy")
    return text("change.keep")


def build_headline(
    sessions: Sequence[SessionLine],
    load: Load | None,
    dist: Distribution | None,
    goal: GoalProgress | None,
    recovery: Recovery | None,
    next_week: NextPlan | None,
    *,
    is_break: bool,
    break_reason: str,
) -> DigestHeadline:
    """Section 1, deterministic: the plan share, the load, the goal, and the one change."""
    counts = tally(sessions)
    if is_break:
        drift = (
            text("headline.ctl_drift", start=load.ctl_start, end=load.ctl_end)
            if load is not None and load.ctl_start is not None and load.ctl_end is not None
            else ""
        )
        first = next_week.sessions[0] if next_week and next_week.sessions else None
        change = text("change.break_return", day=first.day, name=first.name) if first else ""
        return DigestHeadline(
            text_zh=text("headline.break", reason=break_reason, skipped=counts["skipped"]) + drift,
            verdict="break",
            change_zh=change,
        )
    judged, share = plan_share(counts)
    parts = []
    if share is None:
        verdict = "no_data"
        parts.append(text("headline.no_plan", rides=len([s for s in sessions if s.activity_id])))
    else:
        verdict = (
            "on_track"
            if share >= ON_TRACK_SHARE
            else "partly"
            if share >= PARTLY_SHARE
            else "off_track"
        )
        listed = text("common.list_sep").join(
            text("headline.count", name=text(f"verdict.{v}"), n=counts[v])
            for v in ("achieved", "partial", "missed", "overdone", "skipped", "no_data")
            if counts[v]
        )
        parts.append(text(f"headline.{verdict}", judged=judged, counts=listed))
    if load is not None and load.planned_tss:
        parts.append(
            text(
                "headline.load",
                actual=load.actual_tss,
                planned=load.planned_tss,
                ratio=load.actual_tss / load.planned_tss,
            )
        )
    parts.append(goal_clause(goal))
    return DigestHeadline(
        text_zh=join(parts) + text("headline.end"),
        verdict=verdict,  # type: ignore[arg-type]
        change_zh=_change(counts, load, dist, recovery),
    )


# ------------------------------------------------------------------------------- assembly


def _methods(d: WeeklyDigest) -> list[MethodUsed]:
    ids: list[str] = []
    if any(s.activity_id for s in d.sessions):
        ids.append("coggan_np_if_tss")
    if any(s.planned_name and s.activity_id for s in d.sessions):
        ids.append(intent_mod.INTENT_VERSION)
    if d.load is not None:
        ids.append("banister_pmc")
    if d.distribution is not None:
        ids.append("tid_3zone")
    if d.goal is not None:
        ids += [goal_mod.GOAL_VERSION, "eftp"]
    if d.recovery is not None:
        ids.append(readiness_mod.ALGO_VERSION)
        if any(x.feel_gap is not None for x in d.recovery.days):
            ids.append(readiness_mod.ALGO_VERSION_FEEL)
    if d.highlights is not None:
        if d.highlights.climbs:
            ids.append("climb_repeats")
        if d.highlights.durability_ratio is not None:
            ids.append(dur_mod.DURABILITY_VERSION)
    if any(s.outdoor for s in d.sessions):
        ids.append(ctx_mod.CONTEXT_VERSION)
    if d.next_week is not None and d.next_week.sessions:
        ids.append("planner")
    if d.plan_reason is not None:
        ids.append(GATE_VERSION)
    return [method(i) for i in ids]


def build_plan_reason(gate: GateDecision | None) -> PlanReason | None:
    """Why next week is planned so: the progression gate's explanation (``None`` without)."""
    if gate is None or gate.explanation is None:
        return None
    e = gate.explanation
    return PlanReason(
        decision=gate.decision,
        headline_zh=e.headline_zh,
        reasons_zh=[r.text_zh for r in e.because],
        explanation=e.to_json_dict(),
    )


def build(
    inp: DigestInputs,
    week_start: dt.date,
    *,
    generated_at: dt.datetime,
    gate: GateDecision | None = None,
) -> WeeklyDigest:
    """Every section of one ISO week (``week_start`` a Monday).

    ``gate``: the progression gate's decision for the week after (flag
    ``plan.progression_gate``; ``None`` leaves the section out).
    """
    start, end = week_start, week_start + dt.timedelta(days=6)
    period = build_period(inp, start, end, week_label(inp, start))
    sessions = build_sessions(inp, start, end)
    facts = weekly_facts(inp.session, start, reports_dir=inp.reports_dir, cfg=inp.cfg)
    load = build_load(inp, start, end, sessions, warnings=facts["warnings"])
    # A backfilled week's "next week" is already in the past: no plan section then.
    nxt = end + dt.timedelta(days=1)
    next_week = (
        build_next(inp, nxt, end + dt.timedelta(days=7), monthly=False)
        if nxt >= inp.today
        else None
    )
    if period.is_break:
        dist = goal = recovery = highlights = None
    else:
        dist = build_distribution(inp, start)
        goal = build_goal(inp, start, end)
        recovery = build_recovery(inp, start, end)
        highlights = build_highlights(inp, start, end)
    d = WeeklyDigest(
        profile=inp.profile,
        athlete_name=inp.athlete_name,
        generated_at=generated_at,
        period=period,
        headline=build_headline(
            sessions,
            load,
            dist,
            goal,
            recovery,
            next_week,
            is_break=period.is_break,
            break_reason=period.break_reason_zh,
        ),
        sessions=sessions,
        load=load,
        distribution=dist,
        goal=goal,
        recovery=recovery,
        highlights=highlights,
        next_week=next_week,
        plan_reason=build_plan_reason(gate) if nxt >= inp.today else None,
    )
    d.methods = _methods(d)
    return d
