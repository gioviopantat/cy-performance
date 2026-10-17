"""Easy days from the athlete's habit (decision D2, adapt.apply_easy_days, ADR-0019)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from cyp.analysis.personal_scales import Learned
from cyp.planning import adapt
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance

MON = dt.date(2026, 10, 12)
HABIT = Learned(66.0, 10, "learned", 80.0)


def _day(ctx: PlanContext, date: dt.date, tss: float, minutes: int = 120) -> DayPlan:
    d = DayPlan(date=date, role="endurance", max_minutes=minutes)
    assert fill_endurance(d, tss, ctx, "base", prefer_cadence=False)
    return d


def _apply(ctx: PlanContext, days: list[DayPlan], easy: Learned = HABIT, **kw: object) -> list:
    args: dict[str, object] = {
        "mutable_from": MON,
        "skip": set(),
        "ahead_weeks": set(),
        "ctx": ctx,
        "phase_of": lambda d: "base",
    }
    args.update(kw)
    return adapt.apply_easy_days(days, easy, **args)  # type: ignore[arg-type]


def test_spin_becomes_the_habit_ride_with_an_if_cap(ctx: PlanContext) -> None:
    spin = _day(ctx, MON + dt.timedelta(days=1), 19)
    assert spin.template_id == "recovery_spin"
    out = _apply(ctx, [spin])
    assert spin.template_id in adapt.EASY_DAY_POOL and spin.tss > 60
    assert spin.workout is not None and spin.workout.if_ <= adapt.EASY_DAY_IF_CAP
    assert 60 <= spin.minutes <= 90 and spin.role == "endurance"
    assert out and out[0].key == f"plan.learn.easy_day_tss.{spin.date.isoformat()}"
    assert out[0].method.claim == "planning.easy_day_habit"


def test_never_lowers_and_leaves_hard_and_long_days(ctx: PlanContext) -> None:
    long_z2 = _day(ctx, MON + dt.timedelta(days=1), 88)
    before = long_z2.template_id
    assert _apply(ctx, [long_z2]) == [] and long_z2.template_id == before
    hard = DayPlan(date=MON + dt.timedelta(days=2), role="hit", max_minutes=90)
    assert _apply(ctx, [hard]) == []


def test_no_widening_without_a_learned_habit(ctx: PlanContext) -> None:
    spin = _day(ctx, MON + dt.timedelta(days=1), 19)
    assert _apply(ctx, [spin], replace(HABIT, source="default")) == []
    assert spin.template_id == "recovery_spin"


def test_masters_follow_the_habit_with_a_lower_if_cap(ctx: PlanContext) -> None:
    """Roger, 2026-10-17: dad's easy days follow his habit, intensity stays conservative."""
    spin = _day(ctx, MON + dt.timedelta(days=1), 19)
    out = _apply(replace(ctx, masters=True), [spin])
    assert out and spin.template_id in adapt.EASY_DAY_POOL
    assert spin.workout is not None and spin.workout.if_ <= adapt.MASTERS_EASY_DAY_IF_CAP
    assert adapt.MASTERS_EASY_DAY_IF_CAP < adapt.EASY_DAY_IF_CAP
    assert "60 歲以上" in out[0].because[1].text_zh


def test_skips_frozen_today_ahead_weeks_and_short_days(ctx: PlanContext) -> None:
    d = MON + dt.timedelta(days=1)
    for kw in (
        {"mutable_from": d + dt.timedelta(days=1)},
        {"skip": {d}},
        {"ahead_weeks": {MON}},
    ):
        spin = _day(ctx, d, 19)
        assert _apply(ctx, [spin], **kw) == [] and spin.template_id == "recovery_spin"
    short = _day(ctx, d, 19, minutes=50)
    assert _apply(ctx, [short]) == []
    # A yaml value counts like a learned one.
    spin = _day(ctx, d, 19)
    assert _apply(ctx, [spin], Learned(66.0, 0, "yaml"))
