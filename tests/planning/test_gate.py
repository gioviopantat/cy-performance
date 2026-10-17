"""Weekly progression gate (planning/gate.py, ADR-0018): every rule on both sides."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest

from cyp.planning import gate
from cyp.planning.gate import GateDecision, WeekEvidence, decide
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance
from cyp.planning.season import SeasonSkeleton, week_targets

MON = dt.date(2026, 10, 12)
#: A week that earns "up": 2/2 key sessions, easier than usual, HRV in band, on target.
GOOD = WeekEvidence(
    start=MON - dt.timedelta(days=7),
    key_planned=2,
    key_achieved=2,
    rpe_delta=-0.5,
    rpe_n=3,
    hrv_band="within",
    easy_rest_days=1,
    actual_tss=500.0,
    target_tss=500.0,
    days_since_ride=1,
)


def _d(ev: WeekEvidence, prev: GateDecision | None = None, **kw: Any) -> str:
    kw.setdefault("conservative", False)
    kw.setdefault("extras_used", 0)
    return decide(ev, prev, **kw)[0]


def _prev(decision: str, rpe: float | None = -1.5) -> GateDecision:
    return GateDecision(
        week_start=GOOD.start,
        decision=decision,  # type: ignore[arg-type]
        progression_index=3,
        season_index=3,
        evidence=replace(GOOD, rpe_delta=rpe),
    )


# ----------------------------------------------------------------------------- decide


def test_up_needs_every_condition() -> None:
    assert _d(GOOD) == "up"
    for change in (
        {"key_achieved": 1},  # 1/2 achieved
        {"key_planned": 1, "key_achieved": 1},  # fewer than 2 key sessions
        {"rpe_delta": 0.5},
        {"hrv_band": "below"},
        {"easy_rest_days": 2},
        {"actual_tss": 400.0},  # 80 %
        {"actual_tss": 600.0},  # 120 %
        {"target_tss": None},
    ):
        assert _d(replace(GOOD, **change)) == "hold", change  # type: ignore[arg-type]
    # Boundaries count for a normal athlete.
    for edge in ({"rpe_delta": 0.0}, {"actual_tss": 425.0}, {"actual_tss": 575.0}):
        assert _d(replace(GOOD, **edge)) == "up", edge  # type: ignore[arg-type]


def test_conservative_athlete_needs_data_and_a_margin() -> None:
    assert _d(GOOD, conservative=True) == "up"
    for change in (
        {"rpe_delta": None},
        {"hrv_band": None},
        {"rpe_delta": 0.0},
        {"actual_tss": 425.0},
    ):
        assert _d(replace(GOOD, **change), conservative=True) == "hold", change  # type: ignore[arg-type]
    assert _d(replace(GOOD, rpe_delta=None, hrv_band=None)) == "up"  # missing is neutral


def test_extra_step_after_two_easy_ups_with_a_block_cap() -> None:
    easy = replace(GOOD, rpe_delta=-1.2)
    assert _d(easy, _prev("up")) == "extra"
    assert _d(easy, _prev("up", rpe=-0.5)) == "up"  # last week not easy enough
    assert _d(replace(GOOD, rpe_delta=-0.9), _prev("up")) == "up"
    assert _d(easy, _prev("hold")) == "up"
    assert _d(easy, _prev("up"), extras_used=gate.EXTRA_MAX_PER_BLOCK) == "up"
    assert _d(easy, _prev("up"), conservative=True) == "up"


def test_down_rules_both_sides() -> None:
    missed = replace(GOOD, key_achieved=0, key_missed=2, rpe_delta=2.0)
    assert _d(missed) == "down"
    assert _d(replace(missed, rpe_delta=1.9)) == "hold"
    assert _d(replace(missed, key_missed=1)) == "hold"
    assert _d(replace(GOOD, hrv_below_days=5)) == "down"
    assert _d(replace(GOOD, hrv_below_days=4)) == "up"
    assert _d(replace(GOOD, easy_rest_days=3)) == "down"


def test_early_recovery_after_two_downs_or_high_fatigue() -> None:
    down = replace(GOOD, easy_rest_days=3)
    assert _d(down, _prev("down")) == "early_recovery"
    assert _d(down, _prev("hold")) == "down"
    assert _d(replace(down, fatigue_z=1.5)) == "early_recovery"
    assert _d(replace(down, fatigue_z=1.4)) == "down"
    assert _d(replace(GOOD, fatigue_z=3.0)) == "up"  # fatigue alone is not "down"


def test_back_from_a_break() -> None:
    assert _d(replace(GOOD, days_since_ride=10)) == "back_from_break"
    assert _d(replace(GOOD, days_since_ride=9)) == "up"
    _, reasons = decide(replace(GOOD, days_since_ride=12), None, conservative=True, extras_used=0)
    assert reasons[0].evidence["steps"] == gate.BREAK_STEPS
    _, long = decide(replace(GOOD, days_since_ride=22), None, conservative=True, extras_used=0)
    assert long[0].evidence["steps"] == gate.LONG_BREAK_STEPS


# ------------------------------------------------------------------------------ apply


def _week(skeleton: SeasonSkeleton, index: int) -> Any:
    return next(w for w in skeleton.weeks if w.index == index)


def test_steps_move_the_progression_within_bounds(skeleton: SeasonSkeleton) -> None:
    w3 = _week(skeleton, 3)  # base, loading, season index 3
    assert not w3.recovery and w3.progression_index == 3
    prev = GateDecision(w3.start, "up", 2, 2, block_idx=w3.block_idx)
    up = gate._apply(w3, "up", [], GOOD, prev, 0)
    assert up.progression_index == 3 and not up.changes_plan
    hold = gate._apply(w3, "hold", [], GOOD, prev, 0)
    assert hold.progression_index == 2 and hold.changes_plan
    down = gate._apply(w3, "down", [], GOOD, prev, 0)
    assert down.progression_index == 1 and down.tss_factor == gate.DOWN_TSS_FACTOR
    extra = gate._apply(w3, "extra", [], GOOD, prev, 0)
    assert extra.progression_index == 4 and extra.extras_used == 1  # season 3 + 1 extra
    capped = gate._apply(w3, "extra", [], GOOD, GateDecision(w3.start, "up", 3, 3), 0)
    assert capped.progression_index == 4  # never above season index + extras taken
    early = gate._apply(w3, "early_recovery", [], GOOD, prev, 0)
    assert early.recovery and early.progression_index == 2
    away = gate._apply(w3, "back_from_break", [], replace(GOOD, days_since_ride=30), prev, 0)
    assert away.progression_index == 1
    assert down.explanation is not None and down.explanation.key == f"plan.gate.{w3.start}"
    assert down.explanation.method is not None
    assert down.explanation.method.claim == "planning.progression_gate"


def test_season_recovery_and_test_weeks_are_not_changed(skeleton: SeasonSkeleton) -> None:
    rec = next(w for w in skeleton.weeks if w.recovery)
    prev = GateDecision(rec.start, "up", rec.progression_index, rec.progression_index)
    g = gate._apply(rec, "down", [], GOOD, prev, 0)
    assert g.tss_factor == 1.0 and not g.recovery and not g.changes_plan
    first = gate._apply(skeleton.weeks[0], "down", [], GOOD, None, 0)
    assert first.progression_index == 1  # a block start never goes below 1


def test_gated_week_targets_and_projection(skeleton: SeasonSkeleton) -> None:
    w3 = _week(skeleton, 3)
    t = week_targets(w3, 50.0, weekly_max_minutes=900)
    down = GateDecision(w3.start, "down", 2, 3, block_idx=w3.block_idx, tss_factor=0.85)
    assert gate.gated_week(w3, down).progression_index == 2
    assert gate.gated_targets(t, down).target_tss == pytest.approx(t.target_tss * 0.85, abs=0.1)
    assert gate.gated_targets(t, None) is t and gate.gated_week(w3, None) is w3
    early = GateDecision(w3.start, "early_recovery", 3, 3, block_idx=w3.block_idx, recovery=True)
    gw = gate.gated_week(w3, early)
    assert gw.recovery and gw.hit_sessions == max(w3.hit_sessions - 1, 0) and gw.ramp_target == 0
    w4 = _week(skeleton, 4)
    nxt = gate.projected(w4, down)
    assert nxt.progression_index == w4.progression_index - 1
    other_block = next(w for w in skeleton.weeks if w.block_idx != w3.block_idx)
    assert gate.projected(other_block, down).progression_index == other_block.progression_index


# ------------------------------------------------------------------------ accumulation


def _easy(ctx: PlanContext, day: dt.date) -> DayPlan:
    d = DayPlan(date=day, role="endurance", max_minutes=150)
    assert fill_endurance(d, 88, ctx, "base", prefer_cadence=False)
    return d


def test_accumulation_eases_from_friday_when_15_percent_ahead(ctx: PlanContext) -> None:
    fri = MON + dt.timedelta(days=4)
    thu = fri - dt.timedelta(days=1)
    for today, actual, eased in ((fri, 460.0, True), (fri, 455.0, False), (thu, 500.0, False)):
        days = [_easy(ctx, today), _easy(ctx, today + dt.timedelta(days=1))]
        before = [d.tss for d in days]
        out = gate.ease_accumulation(
            days, today, actual_to_date=actual, planned_to_date=400.0, mutable_from=today,
            ctx=ctx, phase="base",
        )  # fmt: skip
        assert bool(out) == eased, (today, actual)
        if eased:
            assert all(d.tss < b for d, b in zip(days, before, strict=True))
            assert out[0].key.startswith("plan.gate.accumulation.")
