"""Weekly progression gate: next week up, hold or down from how last week went (ADR-0018).

Spec adaptive-planning, Phase 2 (flag ``plan.progression_gate``, off by default; decision D3:
fully automatic, conservative bounds, explained in the weekly digest). Each Monday the gate
decides the week that starts that day from the week that just ended, after the Nuuttila 2022
down / hold / up model. Inputs (all from stored data, so a replay gets the same answer):

- **Q, key sessions**: hard / test / long-ride slots of last week's plan and their compliance
  (``planned_workouts.compliance``: intent verdict, skipped), and the mean *personal* RPE delta
  of last week's rated planned rides (RPE − table − the athlete's offset, ADR-0019).
- **R, recovery**: the 7-day HRV band (ADR-0013) on the Monday and on each day of last week,
  readiness EASY / REST days, wellness fatigue of the week as a z against the 60 days before.
- **C, completion**: last week's load / its (gated) target.

Rules, first match:

1. **Back from a break**: no ride for ≥ :data:`BREAK_DAYS` days before Monday → hold and
   −:data:`BREAK_STEPS` step (−:data:`LONG_BREAK_STEPS` after > :data:`LONG_BREAK_DAYS` days).
2. **Down** (any): ≥ :data:`DOWN_MISSED_KEY` key sessions missed with personal RPE delta
   ≥ :data:`DOWN_RPE_DELTA`; HRV below band on ≥ :data:`DOWN_HRV_BELOW_DAYS` days;
   ≥ :data:`DOWN_EASY_REST` EASY / REST days → −1 step and week TSS × :data:`DOWN_TSS_FACTOR`.
   **Early recovery week** instead when the week before was also "down" or the week's fatigue
   z ≥ :data:`EARLY_RECOVERY_FATIGUE_Z`: the week becomes a recovery week (season ×0.6, one hard
   session fewer).
3. **Up** (all): ≥ :data:`UP_KEY_MIN` key sessions planned and every one achieved, personal
   RPE delta ≤ :data:`UP_RPE_DELTA`, HRV band within or above on Monday, ≤ :data:`UP_EASY_REST`
   EASY / REST days, completion in [:data:`UP_COMPLETION_LO`, :data:`UP_COMPLETION_HI`] →
   +1 step (the season's normal advance). Two "up" weeks in a row, both with RPE delta ≤
   :data:`EXTRA_RPE_DELTA` → one extra step, at most :data:`EXTRA_MAX_PER_BLOCK` per block.
4. **Hold** otherwise: the progression does not advance; the week's TSS stays the season's.

Steps move the progression index the planner picks workout parameters with
(``SeasonWeek.progression_index``): never above the season's index plus the extra steps taken in
the block, never below 1, reset at each block start. Season recovery and test weeks keep their
schedule (decision recorded, no change). Conservative athletes (masters, D3): a missing RPE or
HRV input counts against "up", a value exactly on an "up" threshold is not enough, and no extra
steps (review §6.2: learning never makes a masters plan harder than the season's).

Weekly accumulation (decision D1-B, :func:`ease_accumulation`): from Friday, a week whose load
so far is ≥ :data:`ACCUMULATION_OVER` above its plan so far eases the remaining easy days by
that share (at least :data:`~cyp.planning.adapt.OVERDONE_FLOOR` of their load is kept).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml

from cyp.analysis.readiness import BASELINE_DAYS, hrv_window, rpe_offset_of, zscore
from cyp.analysis.readiness_job import _point
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.dataset import Dataset
from cyp.planning.adapt import OVERDONE_FLOOR
from cyp.planning.compliance import KEY_ROLES
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance
from cyp.planning.season import SeasonSkeleton, SeasonWeek, WeekTargets, week_targets

GATE_VERSION = "gate_v1"
Decision = Literal["up", "extra", "hold", "down", "early_recovery", "back_from_break"]

#: "Up" needs at least this many key sessions, every one achieved.
UP_KEY_MIN = 2
#: "Up" needs the week's mean personal RPE delta at or below this.
UP_RPE_DELTA = 0.0
#: "Up" allows at most this many EASY / REST readiness days.
UP_EASY_REST = 1
#: "Up" needs last week's load within this share of its target.
UP_COMPLETION_LO = 0.85
UP_COMPLETION_HI = 1.15
#: Two "up" weeks in a row, both this easy (personal RPE delta), earn one extra step ...
EXTRA_RPE_DELTA = -1.0
#: ... at most this many times per block.
EXTRA_MAX_PER_BLOCK = 2
#: "Down": this many key sessions missed ...
DOWN_MISSED_KEY = 2
#: ... with the week's personal RPE delta at least this high;
DOWN_RPE_DELTA = 2.0
#: or the 7-day HRV below its band on this many days of the week;
DOWN_HRV_BELOW_DAYS = 5
#: or this many EASY / REST readiness days.
DOWN_EASY_REST = 3
#: A "down" week's TSS target is cut to this share.
DOWN_TSS_FACTOR = 0.85
#: "Down" with the week's fatigue z at or above this becomes an early recovery week.
EARLY_RECOVERY_FATIGUE_Z = 1.5
#: No ride for this many days before Monday is a break (Mujika & Padilla 2000: detraining) ...
BREAK_DAYS = 10
#: ... costing this many steps, or this many after more than LONG_BREAK_DAYS days.
BREAK_STEPS = 1
LONG_BREAK_DAYS = 21
LONG_BREAK_STEPS = 2
#: From this weekday (0 = Monday; 4 = Friday) a week ahead of its plan eases its easy days ...
ACCUMULATION_FROM_WEEKDAY = 4
#: ... once its load so far is at least this share above the plan so far.
ACCUMULATION_OVER = 0.15

STEP: dict[Decision, int] = {
    "up": 1,
    "extra": 2,
    "hold": 0,
    "down": -1,
    "early_recovery": 0,
    "back_from_break": -BREAK_STEPS,
}
TEXT_PATH = Path(__file__).resolve().parent / "i18n" / "gate.zh-TW.yaml"


@cache
def _texts() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(TEXT_PATH.read_text(encoding="utf-8"))
    return data


def text(key: str, **values: Any) -> str:
    """zh-TW text ``section.name`` of :data:`TEXT_PATH` formatted with ``values``."""
    section, name = key.split(".", 1)
    template: str = _texts()[section][name]
    return template.format(**values)


def decision_zh(decision: Decision) -> str:
    """The decision in plain zh-TW."""
    return text(f"decision.{decision}", factor=DOWN_TSS_FACTOR)


@dataclass(frozen=True)
class WeekEvidence:
    """What the gate reads about one finished week."""

    start: dt.date
    key_planned: int = 0
    key_achieved: int = 0
    key_missed: int = 0
    rpe_delta: float | None = None
    rpe_n: int = 0
    hrv_band: str | None = None  # on the gate day (the Monday after)
    hrv_below_days: int = 0
    easy_rest_days: int = 0
    actual_tss: float = 0.0
    target_tss: float | None = None
    fatigue_z: float | None = None
    days_since_ride: int | None = None

    @property
    def completion(self) -> float | None:
        """Load / target (``None`` without a target)."""
        if not self.target_tss:
            return None
        return round(self.actual_tss / self.target_tss, 4)


@dataclass(frozen=True)
class GateDecision:
    """The gate's decision for one week and its effect on the plan."""

    week_start: dt.date
    decision: Decision
    #: Progression index the planner uses this week, and the season's own.
    progression_index: int
    season_index: int
    block_idx: int = 0
    tss_factor: float = 1.0
    recovery: bool = False
    extras_used: int = 0
    evidence: WeekEvidence | None = None
    explanation: Explanation | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changes_plan(self) -> bool:
        """Whether the planner plans this week differently from the season skeleton."""
        return (
            self.progression_index != self.season_index or self.tss_factor != 1.0 or self.recovery
        )


# ----------------------------------------------------------------------------- evidence


def _days(start: dt.date, n: int = 7) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range(n)]


def hrv_band(ds: Dataset, day: dt.date) -> str | None:
    """The 7-day HRV band on ``day`` (``None`` when too thin), as readiness reads it."""
    lo = day - dt.timedelta(days=BASELINE_DAYS)
    hist = [_point(w) for d, w in sorted(ds.wellness.items()) if lo <= d < day]
    today = ds.wellness.get(day)
    hw = hrv_window(day, today.hrv if today else None, hist)
    return hw.band if hw is not None else None


def _fatigue_z(ds: Dataset, start: dt.date) -> float | None:
    week = [ds.wellness[d].fatigue for d in _days(start) if d in ds.wellness]
    vals = [v for v in week if v is not None]
    if not vals:
        return None
    lo = start - dt.timedelta(days=BASELINE_DAYS)
    base = [w.fatigue for d, w in ds.wellness.items() if lo <= d < start]
    return zscore(statistics.fmean(vals), base)


def _days_since_ride(ds: Dataset, day: dt.date) -> int | None:
    last = max((a.date for a in ds.activities if a.is_ride and a.date < day), default=None)
    return (day - last).days if last is not None else None


def week_evidence(
    ds: Dataset,
    start: dt.date,
    target_tss: float | None,
    rpe_offset: Mapping[str, float] | None = None,
) -> WeekEvidence:
    """The gate's inputs for the week starting ``start`` (see the module docstring)."""
    days = _days(start)
    key = [
        dict(row.compliance)
        for d in days
        for row in ds.planned.get(d, ())[:1]
        if row.compliance is not None and row.role in KEY_ROLES
    ]
    deltas = [
        float(rec["rpe_delta"]) - rpe_offset_of(rpe_offset, rec.get("rated_class"))
        for d in days
        for row in ds.planned.get(d, ())[:1]
        if (rec := row.compliance) is not None and rec.get("rpe_delta") is not None
    ]
    monday = start + dt.timedelta(days=7)
    recs = [ds.readiness[d].recommendation for d in days if d in ds.readiness]
    return WeekEvidence(
        start=start,
        key_planned=len(key),
        key_achieved=sum(1 for r in key if r.get("intent_verdict") == "achieved"),
        key_missed=sum(1 for r in key if r.get("skipped") or r.get("intent_verdict") == "missed"),
        rpe_delta=round(statistics.fmean(deltas), 2) if deltas else None,
        rpe_n=len(deltas),
        hrv_band=hrv_band(ds, monday),
        hrv_below_days=sum(1 for d in days if hrv_band(ds, d) == "below"),
        easy_rest_days=sum(1 for r in recs if r in ("EASY", "REST")),
        actual_tss=round(sum(ds.loads.get(d, 0.0) for d in days), 1),
        target_tss=target_tss,
        fatigue_z=_fatigue_z(ds, start),
        days_since_ride=_days_since_ride(ds, monday),
    )


# ----------------------------------------------------------------------------- decision


def decide(
    ev: WeekEvidence, prev: GateDecision | None, *, conservative: bool, extras_used: int
) -> tuple[Decision, list[Reason]]:
    """The decision for the week after ``ev`` and one reason per input (rules above)."""
    reasons: list[Reason] = []
    gap = ev.days_since_ride
    if gap is not None and gap >= BREAK_DAYS:
        steps = LONG_BREAK_STEPS if gap > LONG_BREAK_DAYS else BREAK_STEPS
        reasons.append(
            Reason(
                text_zh=text("reason.break", gap=gap, break_days=BREAK_DAYS, steps=steps),
                evidence={"days_since_ride": gap, "break_days": BREAK_DAYS, "steps": steps},
            )
        )
        return "back_from_break", reasons
    down: list[Reason] = []
    if ev.key_missed >= DOWN_MISSED_KEY and (ev.rpe_delta or 0.0) >= DOWN_RPE_DELTA:
        down.append(
            Reason(
                text_zh=text("reason.down_key", missed=ev.key_missed, delta=ev.rpe_delta or 0.0),
                evidence={"key_missed": ev.key_missed, "rpe_delta": ev.rpe_delta},
            )
        )
    if ev.hrv_below_days >= DOWN_HRV_BELOW_DAYS:
        down.append(
            Reason(
                text_zh=text("reason.down_hrv", n=ev.hrv_below_days),
                evidence={"hrv_below_days": ev.hrv_below_days},
            )
        )
    if ev.easy_rest_days >= DOWN_EASY_REST:
        down.append(
            Reason(
                text_zh=text("reason.down_easy", n=ev.easy_rest_days),
                evidence={"easy_rest_days": ev.easy_rest_days},
            )
        )
    if down:
        reasons += down
        tired = ev.fatigue_z is not None and ev.fatigue_z >= EARLY_RECOVERY_FATIGUE_Z
        if (prev is not None and prev.decision == "down") or tired:
            why = (
                text("reason.two_down")
                if prev is not None and prev.decision == "down"
                else text("reason.tired", z=ev.fatigue_z or 0.0)
            )
            reasons.append(
                Reason(
                    text_zh=text("reason.early", why=why),
                    evidence={
                        "previous": prev.decision if prev else None,
                        "fatigue_z": ev.fatigue_z,
                    },
                )
            )
            return "early_recovery", reasons
        return "down", reasons
    checks = _up_checks(ev, conservative)
    reasons += [r for _, r in checks]
    if all(ok for ok, _ in checks):
        extra = (
            not conservative
            and prev is not None
            and prev.decision in ("up", "extra")
            and prev.evidence is not None
            and _le(prev.evidence.rpe_delta, EXTRA_RPE_DELTA)
            and _le(ev.rpe_delta, EXTRA_RPE_DELTA)
            and extras_used < EXTRA_MAX_PER_BLOCK
        )
        if extra:
            reasons.append(
                Reason(
                    text_zh=text(
                        "reason.extra",
                        limit=EXTRA_RPE_DELTA,
                        used=extras_used,
                        max=EXTRA_MAX_PER_BLOCK,
                    ),
                    evidence={"extras_used": extras_used, "max": EXTRA_MAX_PER_BLOCK},
                )
            )
            return "extra", reasons
        return "up", reasons
    return "hold", reasons


def _le(v: float | None, limit: float) -> bool:
    return v is not None and v <= limit


def _up_checks(ev: WeekEvidence, conservative: bool) -> list[tuple[bool, Reason]]:
    """Each "up" condition and whether it holds (conservative: missing / on the line = no)."""
    out: list[tuple[bool, Reason]] = []
    keys_ok = ev.key_planned >= UP_KEY_MIN and ev.key_achieved == ev.key_planned
    out.append(
        (
            keys_ok,
            Reason(
                text_zh=text(
                    "up.keys", achieved=ev.key_achieved, planned=ev.key_planned, min=UP_KEY_MIN
                ),
                evidence={"key_achieved": ev.key_achieved, "key_planned": ev.key_planned},
            ),
        )
    )
    if ev.rpe_delta is None:
        rpe_ok = not conservative
        rpe_text = text("up.rpe_none") + (text("up.conservative") if conservative else "")
    else:
        rpe_ok = ev.rpe_delta < UP_RPE_DELTA if conservative else ev.rpe_delta <= UP_RPE_DELTA
        rpe_text = text("up.rpe", delta=ev.rpe_delta, n=ev.rpe_n, limit=UP_RPE_DELTA)
    out.append((rpe_ok, Reason(text_zh=rpe_text, evidence={"rpe_delta": ev.rpe_delta})))
    if ev.hrv_band is None:
        hrv_ok = not conservative
        hrv_text = text("up.hrv_none") + (text("up.conservative") if conservative else "")
    else:
        hrv_ok = ev.hrv_band in ("within", "above")
        hrv_text = text("up.hrv", band=text(f"band.{ev.hrv_band}"))
    out.append((hrv_ok, Reason(text_zh=hrv_text, evidence={"hrv_band": ev.hrv_band})))
    out.append(
        (
            ev.easy_rest_days <= UP_EASY_REST,
            Reason(
                text_zh=text("up.easy", n=ev.easy_rest_days, max=UP_EASY_REST),
                evidence={"easy_rest_days": ev.easy_rest_days},
            ),
        )
    )
    c = ev.completion
    if c is None:
        comp_ok = False
    elif conservative:
        comp_ok = UP_COMPLETION_LO < c < UP_COMPLETION_HI
    else:
        comp_ok = UP_COMPLETION_LO <= c <= UP_COMPLETION_HI
    comp_text = (
        text(
            "up.completion",
            c=c,
            actual=ev.actual_tss,
            target=ev.target_tss,
            lo=UP_COMPLETION_LO,
            hi=UP_COMPLETION_HI,
        )
        if c is not None and ev.target_tss
        else text("up.no_target")
    )
    out.append(
        (comp_ok, Reason(text_zh=comp_text, evidence={"completion": round(c, 3) if c else None}))
    )
    return out


# -------------------------------------------------------------------------------- chain


def gate_chain(
    ds: Dataset,
    sk: SeasonSkeleton,
    weekly_max_minutes: int,
    *,
    until: dt.date,
    rpe_offset: Mapping[str, float] | None = None,
    conservative: bool = False,
) -> dict[dt.date, GateDecision]:
    """Decisions for every season week from week 2 through the week containing ``until``.

    Walks the season from its first week: each week's load target is recomputed as the planner
    set it on its Monday (CTL going into the week, the previous loading week, this chain's own
    decision), so completion compares like with like. Deterministic: the same stored data gives
    the same decisions every morning, and a replay gets what the live run got.
    """
    out: dict[dt.date, GateDecision] = {}
    targets: dict[dt.date, float] = {}
    prev: GateDecision | None = None
    prev_loading: float | None = None
    for i, w in enumerate(sk.weeks):
        if w.start > until:
            break
        g: GateDecision | None = None
        if i > 0:
            last = sk.weeks[i - 1]
            ev = week_evidence(ds, last.start, targets.get(last.start), rpe_offset)
            same_block = last.block_idx == w.block_idx and prev is not None
            extras = prev.extras_used if same_block and prev is not None else 0
            decision, reasons = decide(ev, prev, conservative=conservative, extras_used=extras)
            g = _apply(w, decision, reasons, ev, prev if same_block else None, extras)
            out[w.start] = prev = g
        gw = gated_week(w, g)
        ctl, atl = ds.ctl_atl(w.start - dt.timedelta(days=1))
        t = gated_targets(
            week_targets(
                gw,
                ctl,
                weekly_max_minutes=weekly_max_minutes,
                prev_loading_tss=prev_loading,
                atl_start=atl,
            ),
            g,
        )
        targets[w.start] = t.target_tss
        if not gw.recovery and gw.phase != "test":
            prev_loading = t.target_tss
    return out


def _apply(
    w: SeasonWeek,
    decision: Decision,
    reasons: list[Reason],
    ev: WeekEvidence,
    prev: GateDecision | None,
    extras: int,
) -> GateDecision:
    """Turn a decision into the week's progression index, TSS factor and explanation."""
    fixed = w.recovery or w.phase == "test"
    base = prev.progression_index if prev is not None else w.progression_index - 1
    step = STEP[decision]
    if (
        decision == "back_from_break"
        and ev.days_since_ride
        and ev.days_since_ride > LONG_BREAK_DAYS
    ):
        step = -LONG_BREAK_STEPS
    if decision == "extra":
        extras += 1
    if fixed:
        index = prev.progression_index if prev is not None else w.progression_index
        factor, recovery = 1.0, False
    else:
        index = max(1, min(base + step, w.progression_index + extras))
        factor = DOWN_TSS_FACTOR if decision == "down" else 1.0
        recovery = decision == "early_recovery"
    headline = text("week.headline", start=w.start.isoformat(), decision=decision_zh(decision))
    if fixed:
        headline += text("week.fixed_suffix")
    because = list(reasons)
    because.append(
        Reason(
            text_zh=(
                text("week.effect", index=index, season=w.progression_index)
                + (text("week.effect_factor", factor=factor) if factor != 1.0 else "")
                + (text("week.effect_recovery") if recovery else "")
                + (text("week.fixed_note") if fixed else "")
            ),
            evidence={
                "progression_index": index,
                "season_index": w.progression_index,
                "tss_factor": factor,
                "recovery": recovery,
            },
        )
    )
    expl = Explanation(
        key=f"plan.gate.{w.start.isoformat()}",
        headline_zh=headline,
        because=because,
        method=MethodRef(
            model_id="progression_gate",
            version=GATE_VERSION,
            inputs=_evidence_dict(ev),
            doc="docs/adr/0018-progression-gate.md",
            claim="planning.progression_gate",
        ),
        confidence="medium",
        glossary_terms=["periodization_3_1", "readiness_v1", "guardrails"],
    )
    return GateDecision(
        week_start=w.start,
        decision=decision,
        progression_index=index,
        season_index=w.progression_index,
        block_idx=w.block_idx,
        tss_factor=factor,
        recovery=recovery,
        extras_used=extras,
        evidence=ev,
        explanation=expl,
        reasons=tuple(r.text_zh for r in because),
    )


def _evidence_dict(ev: WeekEvidence) -> dict[str, Any]:
    return {
        "week": ev.start.isoformat(),
        "key_planned": ev.key_planned,
        "key_achieved": ev.key_achieved,
        "key_missed": ev.key_missed,
        "rpe_delta": ev.rpe_delta,
        "hrv_band": ev.hrv_band,
        "hrv_below_days": ev.hrv_below_days,
        "easy_rest_days": ev.easy_rest_days,
        "actual_tss": ev.actual_tss,
        "target_tss": ev.target_tss,
        "fatigue_z": round(ev.fatigue_z, 2) if ev.fatigue_z is not None else None,
        "days_since_ride": ev.days_since_ride,
    }


# ----------------------------------------------------------------------- planner hooks


def gated_week(w: SeasonWeek, g: GateDecision | None) -> SeasonWeek:
    """The season week as the planner should plan it under decision ``g``."""
    if g is None or not g.changes_plan:
        return w
    if g.recovery:
        return replace(
            w,
            recovery=True,
            hit_sessions=max(w.hit_sessions - 1, 0),
            ramp_target=0.0,
            progression_index=g.progression_index,
        )
    return replace(w, progression_index=g.progression_index)


def projected(w: SeasonWeek, last: GateDecision) -> GateDecision:
    """A week after the last decided one (assumed a normal advance).

    In the same block it keeps the decided offset from the season's index; a new block starts
    from the season's.
    """
    same = w.block_idx == last.block_idx
    offset = last.progression_index - last.season_index if same else 0
    extras = last.extras_used if same else 0
    return GateDecision(
        week_start=w.start,
        decision="up",
        progression_index=max(1, min(w.progression_index + offset, w.progression_index + extras)),
        season_index=w.progression_index,
        block_idx=w.block_idx,
        extras_used=extras,
    )


def gated_targets(t: WeekTargets, g: GateDecision | None) -> WeekTargets:
    """``t`` with a "down" week's TSS factor applied and explained."""
    if g is None or g.tss_factor == 1.0:
        return t
    tss = round(t.target_tss * g.tss_factor, 1)
    reason = Reason(
        text_zh=text("targets.reason", factor=g.tss_factor, before=t.target_tss, after=tss),
        evidence={"tss_factor": g.tss_factor, "before": t.target_tss},
    )
    expl = t.explanation.model_copy(
        update={
            "because": [*t.explanation.because, reason],
            "headline_zh": t.explanation.headline_zh
            + text("targets.headline", factor=g.tss_factor),
        }
    )
    return replace(
        t,
        target_tss=tss,
        target_hours=round(t.target_hours * g.tss_factor, 2),
        explanation=expl,
    )


def ease_accumulation(
    days: Sequence[DayPlan],
    today: dt.date,
    *,
    actual_to_date: float,
    planned_to_date: float,
    mutable_from: dt.date,
    ctx: PlanContext,
    phase: str,
) -> list[Explanation]:
    """Decision D1-B: from Friday, a week ahead of its plan eases its remaining easy days."""
    if today.weekday() < ACCUMULATION_FROM_WEEKDAY or planned_to_date <= 0:
        return []
    over = round(actual_to_date / planned_to_date - 1.0, 4)
    if over < ACCUMULATION_OVER:
        return []
    share = min(over, 1.0 - OVERDONE_FLOOR)
    sunday = today + dt.timedelta(days=6 - today.weekday())
    out: list[Explanation] = []
    for d in days:
        if not (max(today, mutable_from) <= d.date <= sunday) or d.workout is None:
            continue
        if d.is_hard or d.role not in ("endurance", "recovery"):
            continue
        before, before_tss = d.template_id, d.tss
        target = before_tss * (1.0 - share)
        if not fill_endurance(d, target, ctx, phase, prefer_cadence=False) or d.tss >= before_tss:
            continue
        d.notes.append(text("accumulation.note", over=over, share=share))
        out.append(
            Explanation(
                key=f"plan.gate.accumulation.{d.date.isoformat()}",
                headline_zh=text(
                    "accumulation.headline",
                    date=d.date.isoformat(),
                    before=before,
                    after=d.template_id,
                ),
                because=[
                    Reason(
                        text_zh=text(
                            "accumulation.reason",
                            actual=actual_to_date,
                            planned=planned_to_date,
                            over=over,
                            limit=ACCUMULATION_OVER,
                        ),
                        evidence={
                            "actual_to_date": round(actual_to_date, 1),
                            "planned_to_date": round(planned_to_date, 1),
                            "over": round(over, 3),
                            "share": round(share, 3),
                        },
                    )
                ],
                method=MethodRef(
                    model_id="progression_gate.accumulation",
                    version=GATE_VERSION,
                    inputs={},
                    doc="docs/adr/0018-progression-gate.md",
                    claim="planning.progression_gate",
                ),
                confidence="medium",
                glossary_terms=["guardrails"],
            )
        )
    return out
