"""Hard guardrails applied after every planner / adaptation pass (docs/05 §5, glossary guardrails).

:func:`check` returns :class:`Violation` objects with rule ids; :func:`enforce` repairs the
mutable days until clean (or until nothing repairable is left, then the remaining violations
are returned and the run is flagged ``needs_review``). Repairs, in order of preference:

- TSS-type rules (``ramp_rate``, ``tsb_floor``, ``weekly_tss_vs_mean``): shorten the
  lowest-value non-rest day (endurance before long ride before HIT/test; later days first)
  one template size down, or rest it if it is already the smallest.
- ``hit_spacing`` / ``hit_per_week``: downgrade the later hard day to Z2 of the same length.
- ``single_ride``: shorten that day.
- ``rest_days``: rest the lowest-value day of the week.

Every repair is recorded with an Explanation (key ``plan.repair.<rule>.<date>``).
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.timeutil import week_start
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance

GUARDRAILS_VERSION = "guardrails_v1"
DEFAULT_RAMP_CAP = 6.0
DEFAULT_TSB_FLOOR = -30.0
WEEKLY_TSS_FACTOR = 1.15
SINGLE_RIDE_FACTOR = 1.6
LONG_RIDE_HIT_GAP_TSS = 150.0
MAX_REPAIRS = 60


@dataclass(frozen=True)
class Violation:
    """One broken rule."""

    rule: str
    date: dt.date
    detail_zh: str
    evidence: dict[str, float | str | None] = field(default_factory=dict)


@dataclass
class Repair:
    """What :func:`enforce` changed to fix a violation."""

    violation: Violation
    date: dt.date
    before: str | None
    after: str | None
    explanation: Explanation


@dataclass
class GuardrailInputs:
    """History and limits the rules need (``phase_of`` maps a date to its phase)."""

    seed: PMCState  # CTL/ATL on the day before the first planned day
    phase_of: Callable[[dt.date], str]
    recovery_weeks: frozenset[dt.date] = frozenset()  # Mondays of recovery / test weeks
    history_loads: Mapping[dt.date, float] = field(default_factory=dict)  # actual, before seed
    longest_ride_min_6w: float | None = None
    low_readiness_days: frozenset[dt.date] = frozenset()  # readiness < 40
    ramp_cap: Mapping[str, float] = field(default_factory=dict)
    tsb_floor: Mapping[str, float] = field(default_factory=dict)
    hit_per_week: Mapping[str, int] = field(default_factory=dict)


def check(days: Sequence[DayPlan], gi: GuardrailInputs) -> list[Violation]:
    """All violations in the horizon, in date order."""
    out: list[Violation] = []
    days = sorted(days, key=lambda d: d.date)
    if not days:
        return out
    traj = simulate(gi.seed, [d.tss for d in days])
    ctl_by: dict[dt.date, float] = {gi.seed.date: gi.seed.ctl}
    for d, st in zip(days, traj, strict=True):
        ctl_by[d.date] = st.ctl
        phase = gi.phase_of(d.date)
        floor = gi.tsb_floor.get(phase, DEFAULT_TSB_FLOOR)
        if st.tsb < floor:
            out.append(
                Violation(
                    "tsb_floor",
                    d.date,
                    f"TSB {st.tsb:.1f} 低於下限 {floor:g}",
                    {"tsb": round(st.tsb, 1), "floor": floor},
                )
            )
        week_ago = ctl_by.get(d.date - dt.timedelta(days=7))
        cap = gi.ramp_cap.get(phase, DEFAULT_RAMP_CAP)
        if week_ago is not None and st.ctl - week_ago > cap:
            out.append(
                Violation(
                    "ramp_rate",
                    d.date,
                    f"CTL 7 天升 {st.ctl - week_ago:.1f}，超過 {cap:g}",
                    {"ramp": round(st.ctl - week_ago, 2), "cap": cap},
                )
            )

    hard = [d for d in days if d.is_hard]
    for a, b in itertools.pairwise(hard):
        if (b.date - a.date).days < 2:
            out.append(
                Violation(
                    "hit_spacing",
                    b.date,
                    f"高強度 {a.date.isoformat()} 與 {b.date.isoformat()} 間隔不足 48 小時",
                    {"prev": a.date.isoformat()},
                )
            )
    for d in days:
        nxt = next((x for x in days if x.date == d.date + dt.timedelta(days=1)), None)
        if d.role == "long_ride" and d.tss > LONG_RIDE_HIT_GAP_TSS and nxt and nxt.is_hard:
            out.append(
                Violation(
                    "hit_spacing",
                    nxt.date,
                    f"前一天長騎 {d.tss:.0f} TSS > {LONG_RIDE_HIT_GAP_TSS:.0f}，隔天不排高強度",
                    {"long_ride_tss": round(d.tss)},
                )
            )

    weeks: dict[dt.date, list[DayPlan]] = {}
    for d in days:
        weeks.setdefault(week_start(d.date), []).append(d)
    hist_weeks = _history_weeks(gi.history_loads)
    for monday, wdays in sorted(weeks.items()):
        phase = gi.phase_of(monday)
        limit = gi.hit_per_week.get(phase)
        n_hard = sum(1 for d in wdays if d.is_hard)
        if limit is not None and n_hard > max(limit, 1):
            out.append(
                Violation(
                    "hit_per_week",
                    max(d.date for d in wdays if d.is_hard),
                    f"本週高強度 {n_hard} 次，上限 {limit}",
                    {"n": n_hard, "limit": limit},
                )
            )
        prior = [hist_weeks[m] for m in sorted(hist_weeks) if m < monday][-4:]
        actual_before = sum(v for k, v in gi.history_loads.items() if monday <= k < wdays[0].date)
        week_tss = actual_before + sum(d.tss for d in wdays)
        if len(prior) == 4 and monday not in gi.recovery_weeks:
            mean = sum(prior) / 4
            if mean > 0 and week_tss > WEEKLY_TSS_FACTOR * mean:
                out.append(
                    Violation(
                        "weekly_tss_vs_mean",
                        wdays[-1].date,
                        f"本週 {week_tss:.0f} TSS 超過近 4 週平均 {mean:.0f} 的 115 %",
                        {"week_tss": round(week_tss), "mean_4w": round(mean)},
                    )
                )
        full_week = len(wdays) == 7
        n_rest = sum(1 for d in wdays if d.role == "rest")
        need = 2 if sum(1 for d in gi.low_readiness_days if week_start(d) == monday) >= 2 else 1
        if full_week and n_rest < need:
            out.append(
                Violation(
                    "rest_days",
                    wdays[-1].date,
                    f"本週休息 {n_rest} 天，至少要 {need} 天",
                    {"rest": n_rest, "need": need},
                )
            )
    if gi.longest_ride_min_6w:
        cap_min = SINGLE_RIDE_FACTOR * gi.longest_ride_min_6w
        for d in days:
            if d.minutes > cap_min:
                out.append(
                    Violation(
                        "single_ride",
                        d.date,
                        f"{d.minutes} 分鐘超過近 6 週最長 {gi.longest_ride_min_6w:.0f} 分 × 1.6",
                        {"minutes": d.minutes, "cap": round(cap_min)},
                    )
                )
    return out


def _history_weeks(loads: Mapping[dt.date, float]) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for d, v in loads.items():
        out[week_start(d)] = out.get(week_start(d), 0.0) + v
    return out


def _shrink(day: DayPlan, ctx: PlanContext, phase: str) -> bool:
    """One step down: smaller endurance template, or rest. Returns False if already rest."""
    if day.workout is None:
        return False
    current = day.tss
    target = current * 0.6
    if (
        day.role in ("endurance", "recovery", "long_ride")
        and fill_endurance(day, target, ctx, phase, prefer_cadence=False)
        and day.tss < current - 1
    ):
        return True
    day.set_rest("為了守住負荷規則，這天改成休息")
    return True


def _downgrade(day: DayPlan, ctx: PlanContext, phase: str) -> bool:
    if day.workout is None:
        return False
    if fill_endurance(day, day.minutes * 0.726, ctx, phase, prefer_cadence=False):
        day.notes.append("高強度降級為 Z2（守住 48 小時間隔／每週次數）")
        return True
    day.set_rest("高強度降級，沒有合適的 Z2 時長，改休息")
    return True


def enforce(
    days: list[DayPlan],
    gi: GuardrailInputs,
    ctx: PlanContext,
    *,
    mutable_from: dt.date,
) -> tuple[list[Repair], list[Violation]]:
    """Repair until clean; returns ``(repairs, remaining violations)``."""
    repairs: list[Repair] = []
    for _ in range(MAX_REPAIRS):
        violations = check(days, gi)
        if not violations:
            return repairs, []
        fixed = False
        for v in violations:
            target = _target_for(v, days, mutable_from)
            if target is None:
                continue
            before = target.template_id
            phase = gi.phase_of(target.date)
            changed = (
                _downgrade(target, ctx, phase)
                if v.rule in ("hit_spacing", "hit_per_week")
                else _shrink(target, ctx, phase)
            )
            if not changed:
                continue
            repairs.append(
                Repair(
                    v, target.date, before, target.template_id, _explain_repair(v, target, before)
                )
            )
            fixed = True
            break
        if not fixed:
            return repairs, violations
    return repairs, check(days, gi)


def _target_for(v: Violation, days: list[DayPlan], mutable_from: dt.date) -> DayPlan | None:
    mutable = [d for d in days if d.date >= mutable_from and d.workout is not None]
    if v.rule in ("hit_spacing", "hit_per_week", "single_ride"):
        return next((d for d in mutable if d.date == v.date), None)
    if v.rule in ("rest_days", "weekly_tss_vs_mean"):
        week = [d for d in mutable if week_start(d.date) == week_start(v.date)]
    else:  # ramp_rate / tsb_floor: anything up to the violating day
        week = [d for d in mutable if d.date <= v.date]
    if not week:
        return None
    return min(week, key=lambda d: (d.value, -d.date.toordinal()))


def _explain_repair(v: Violation, day: DayPlan, before: str | None) -> Explanation:
    after = day.template_id or "休息"
    return Explanation(
        key=f"plan.repair.{v.rule}.{day.date.isoformat()}",
        headline_zh=f"{day.date.isoformat()}：{before or '休息'} → {after}（規則 {v.rule}）",
        because=[
            Reason(text_zh=v.detail_zh, evidence=dict(v.evidence)),
            Reason(
                text_zh=f"移除順序依價值（{day.role} 價值 {day.value:.1f}），最低的先動",
                evidence={"role": day.role, "value": day.value},
            ),
        ],
        method=MethodRef(
            model_id=f"guardrail.{v.rule}",
            version=GUARDRAILS_VERSION,
            inputs={"date": day.date.isoformat()},
            doc="docs/glossary/guardrails.md",
        ),
        confidence="high",
        glossary_terms=["guardrails"],
    )
