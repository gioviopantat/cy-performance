"""Season skeleton, week targets, week planning, guardrails, adaptation."""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.planning import adapt
from cyp.planning.guardrails import GuardrailInputs, check, enforce
from cyp.planning.planner import (
    DayPlan,
    PlanContext,
    assign_roles,
    availability,
    plan_week,
    progression_sequence,
)
from cyp.planning.season import (
    apply_checkpoint,
    build_skeleton,
    checkpoint_missed,
    season_projection,
    week_targets,
)
from cyp.planning.templates import Template
from cyp.settings import AthleteConfig, SeasonConfig

# ------------------------------------------------------------------------------- season


def test_skeleton_matches_docs_05(cfg: AthleteConfig) -> None:
    sk = build_skeleton(cfg)
    assert len(sk.weeks) == 26 and sk.start == dt.date(2026, 10, 5)
    phases = [w.phase for w in sk.weeks]
    assert phases == ["base"] * 8 + ["build"] * 8 + ["threshold"] * 8 + ["test"] * 2
    assert [w.index for w in sk.weeks if w.recovery] == [4, 8, 12, 16, 20, 24]
    tests = {w.index: w.test for w in sk.weeks if w.test}
    assert tests == {
        4: "ramp",
        8: "twenty_min",
        12: "ramp",
        16: "twenty_min",
        20: "ramp",
        24: "twenty_min",
        26: "twenty_min",
    }
    assert [w.hit_sessions for w in sk.weeks[:4]] == [1, 1, 1, 0]
    assert sk.weeks[8].hit_sessions == 2 and sk.weeks[11].hit_sessions == 1
    assert sk.week_of(dt.date(2027, 4, 2)) is sk.weeks[-1]
    assert sk.week_of(dt.date(2026, 10, 4)) is None


def test_shorter_season_shrinks_base_first(cfg: AthleteConfig) -> None:
    short = cfg.model_copy(
        update={"season": SeasonConfig(start=dt.date(2026, 12, 28), type="ftp_target")}
    )
    sk = build_skeleton(short)
    counts = {p: sum(1 for w in sk.weeks if w.phase == p) for p in ("base", "build", "threshold")}
    # 14 weeks: test 2, threshold 8, base keeps its minimum 4, build is squeezed out.
    assert len(sk.weeks) == 14
    assert counts == {"base": 4, "build": 0, "threshold": 8}


def test_week_targets_ramp_recovery_and_cap(cfg: AthleteConfig) -> None:
    sk = build_skeleton(cfg)
    t1 = week_targets(sk.weeks[0], 45.0, weekly_max_minutes=900)
    assert t1.ctl_end == pytest.approx(49.0, abs=0.3)  # +4 per base week
    assert 10 < t1.target_hours < 12 and not t1.capped_by_hours
    rec = week_targets(sk.weeks[3], 57.0, weekly_max_minutes=900, prev_loading_tss=550.0)
    assert rec.target_tss == pytest.approx(330.0)
    capped = week_targets(sk.weeks[14], 85.0, weekly_max_minutes=900)
    assert capped.capped_by_hours and capped.target_hours == pytest.approx(15.0)
    proj = season_projection(sk, 45.0, weekly_max_minutes=900)
    assert max(t.target_hours for t in proj) <= 15.0 + 1e-6
    assert proj[23].ctl_end > proj[0].ctl_end + 25


def test_checkpoint_miss_repeats_next_block(cfg: AthleteConfig) -> None:
    assert checkpoint_missed(258, 249) and not checkpoint_missed(258, 252)
    repeat = apply_checkpoint(cfg, {8: 241.0})  # expected 258 at week 8
    assert repeat == frozenset({1})
    sk = build_skeleton(cfg, repeat_blocks=repeat)
    build = sk.block_weeks(1)
    assert [w.progression_index for w in build] == [1, 2, 1, 1, 2, 3, 4, 4]
    assert apply_checkpoint(cfg, {8: 270.0}) == frozenset()


# ------------------------------------------------------------------------------ planner


def _week_plan(cfg: AthleteConfig, ctx: PlanContext, idx: int) -> list[DayPlan]:
    sk = build_skeleton(cfg)
    w = sk.weeks[idx - 1]
    return plan_week(
        w, week_targets(w, 50.0 + idx, weekly_max_minutes=900, prev_loading_tss=600.0), ctx
    )


def test_roles_respect_availability_and_spacing(cfg: AthleteConfig, ctx: PlanContext) -> None:
    sk = build_skeleton(cfg)
    w = sk.weeks[8]  # build week 1: 2 HIT
    roles = assign_roles(w, availability(w, ctx), ctx)
    by_wd = {d.strftime("%a"): r for d, r in roles.items()}
    assert by_wd == {
        "Mon": "rest",
        "Tue": "hit",
        "Wed": "endurance",
        "Thu": "hit",
        "Fri": "endurance",
        "Sat": "long_ride",
        "Sun": "endurance",
    }


def test_plan_week_is_deterministic_and_explained(cfg: AthleteConfig, ctx: PlanContext) -> None:
    a = _week_plan(cfg, ctx, 9)
    b = _week_plan(cfg, ctx, 9)
    assert [(d.template_id, d.params) for d in a] == [(d.template_id, d.params) for d in b]
    tue = a[1]
    assert tue.template_id == "threshold_3x12" and tue.is_hard
    assert a[5].template_id == "long_ride_late_ss" and a[5].outdoor
    assert all(d.explanation is not None and d.explanation.key == f"plan.day.{d.date}" for d in a)
    assert all(d.minutes <= d.max_minutes for d in a)
    assert a[0].workout is None and "沒有可用時間" in a[0].explanation.because[0].text_zh


def test_test_weeks_place_tests_indoor(cfg: AthleteConfig, ctx: PlanContext) -> None:
    wk4 = _week_plan(cfg, ctx, 4)
    ramp = next(d for d in wk4 if d.role == "test")
    assert ramp.date.strftime("%a") == "Thu" and ramp.template_id == "ramp_test"
    assert not ramp.outdoor
    assert not any(d.role == "hit" for d in wk4)
    wk26 = _week_plan(cfg, ctx, 26)
    test = next(d for d in wk26 if d.role == "test")
    assert test.date == dt.date(2027, 4, 2) and test.template_id == "ftp_test_20min"
    eve = next(d for d in wk26 if d.date == dt.date(2027, 4, 1))
    assert eve.template_id == "openers"


def test_progression_grows_first_param_first(library: dict[str, Template]) -> None:
    seq = progression_sequence(library["ss_3x_n"])
    assert seq[0] == {"reps": 3, "work_min": 8, "pct": 88, "rest_min": 4}
    work = [s["work_min"] for s in seq if s["reps"] == 3 and s["pct"] == 88]
    assert work == [8, 10, 12, 14, 16, 18, 20]
    assert seq[-1] == {"reps": 4, "work_min": 20, "pct": 92, "rest_min": 4}


def test_limiter_bias_picks_alternative(cfg: AthleteConfig, library: dict[str, Template]) -> None:
    sk = build_skeleton(cfg)
    w = sk.weeks[12]  # build week 5: B slot offers over_unders / climb_repeats
    t = week_targets(w, 75.0, weekly_max_minutes=900)
    plain = plan_week(w, t, PlanContext(cfg=cfg, library=library))
    biased = plan_week(
        w, t, PlanContext(cfg=cfg, library=library, bias={"threshold": 1.0, "vo2": 1.0})
    )
    assert [d.template_id for d in plain] == [d.template_id for d in biased]
    ids = {d.template_id for d in plain}
    assert "over_unders_3x_n" in ids or "climb_repeats_goal_power" in ids


def test_overrides_turn_days_off(cfg: AthleteConfig, library: dict[str, Template]) -> None:
    sk = build_skeleton(cfg)
    w = sk.weeks[8]
    tue = w.start + dt.timedelta(days=1)
    ctx = PlanContext(cfg=cfg, library=library, overrides={tue: 0})
    days = plan_week(w, week_targets(w, 70.0, weekly_max_minutes=900), ctx)
    assert days[1].workout is None
    hard = [d.date for d in days if d.is_hard]
    assert len(hard) == 2 and all(
        abs((a - b).days) >= 2 for a, b in itertools.combinations(hard, 2)
    )


# ---------------------------------------------------------------------------- guardrails


def _gi(cfg: AthleteConfig, seed: PMCState, **kw: object) -> GuardrailInputs:
    sk = build_skeleton(cfg)
    return GuardrailInputs(
        seed=seed,
        phase_of=lambda d: (sk.week_of(d) or sk.weeks[0]).phase,
        recovery_weeks=frozenset(w.start for w in sk.weeks if w.recovery or w.phase == "test"),
        ramp_cap=cfg.planner.ramp_cap,
        tsb_floor=cfg.planner.tsb_floor,
        hit_per_week=cfg.planner.hit_per_week,
        **kw,  # type: ignore[arg-type]
    )


def test_full_season_simulation_is_guardrail_clean(cfg: AthleteConfig, ctx: PlanContext) -> None:
    """docs/05 §8: whole season planned as-if-complied -> no violations, HIT ≥ 48 h apart."""
    sk = build_skeleton(cfg)
    ctl = atl = 45.0
    prev = None
    days: list[DayPlan] = []
    for w in sk.weeks:
        t = week_targets(w, ctl, weekly_max_minutes=900, prev_loading_tss=prev, atl_start=atl)
        wk = plan_week(w, t, ctx)
        days += wk
        end = simulate(PMCState(w.start - dt.timedelta(days=1), ctl, atl), [d.tss for d in wk])[-1]
        ctl, atl = end.ctl, end.atl
        if not w.recovery and w.phase != "test":
            prev = t.target_tss
        assert abs(sum(d.tss for d in wk) - t.target_tss) / t.target_tss < 0.15, w.index
        assert sum(d.minutes for d in wk) <= 900
    gi = _gi(cfg, PMCState(sk.start - dt.timedelta(days=1), 45.0, 45.0))
    assert check(days, gi) == []
    hard = [d.date for d in days if d.is_hard]
    assert all((b - a).days >= 2 for a, b in itertools.pairwise(hard))
    assert ctl > 75  # CTL built through the season


def test_guardrails_detect_and_repair(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    # Force a spacing violation: make Wednesday a copy of Tuesday's HIT.
    tue, wed = days[1], days[2]
    wed.role, wed.template_id, wed.workout, wed.params, wed.intent = (
        "hit",
        tue.template_id,
        tue.workout,
        dict(tue.params),
        tue.intent,
    )
    # Very low seed CTL/ATL -> huge ramp; weekly mean small -> weekly_tss_vs_mean.
    hist = {days[0].date - dt.timedelta(days=i): 40.0 for i in range(1, 29)}
    gi = _gi(
        cfg,
        PMCState(days[0].date - dt.timedelta(days=1), 30.0, 30.0),
        history_loads=hist,
        longest_ride_min_6w=100.0,
    )
    rules = {v.rule for v in check(days, gi)}
    assert {"hit_spacing", "ramp_rate", "weekly_tss_vs_mean", "single_ride"} <= rules
    repairs, remaining = enforce(days, gi, ctx, mutable_from=days[0].date)
    assert remaining == [] and repairs
    assert any(r.violation.rule == "hit_spacing" and r.date == wed.date for r in repairs)
    assert all(r.explanation.key.startswith("plan.repair.") for r in repairs)
    assert days[1].template_id == "threshold_3x12"  # higher-value Tuesday HIT kept
    assert check(days, gi) == []


def test_guardrails_never_touch_frozen_days(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    gi = _gi(cfg, PMCState(days[0].date - dt.timedelta(days=1), 10.0, 10.0))
    before = [d.template_id for d in days]
    _, remaining = enforce(days, gi, ctx, mutable_from=days[-1].date + dt.timedelta(days=1))
    assert remaining and [d.template_id for d in days] == before


# ------------------------------------------------------------------------------ adaptation


def test_calendar_collisions(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    sat = days[5].date
    out = adapt.apply_calendar(days, {sat: ["RACE_B"], days[2].date: ["NOTE"]})
    assert days[5].workout is None and len(out) == 1 and "RACE_B" in out[0].headline_zh
    assert days[2].workout is not None  # plain notes do not block


def test_readiness_rest_moves_hit_and_easy_caps(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    tue = days[1].date
    out = adapt.apply_readiness(
        days,
        tue,
        "REST",
        tsb=-5,
        hard_yesterday=False,
        ctx=ctx,
        phase="build",
        progress=0.0,
        progress_step=0.2,
    )
    assert days[1].workout is None
    assert any(
        e.key.startswith("plan.adapt.reslot") or e.key.startswith("plan.adapt.drop") for e in out
    )
    days = _week_plan(cfg, ctx, 9)
    planned = days[1].tss
    adapt.apply_readiness(
        days,
        tue,
        "EASY",
        tsb=-5,
        hard_yesterday=False,
        ctx=ctx,
        phase="build",
        progress=0.0,
        progress_step=0.2,
    )
    assert not days[1].is_hard and days[1].tss <= planned * 0.6 + 25


def test_readiness_upgrade_only_when_fresh(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    tue = days[1].date
    before = dict(days[1].params)
    assert (
        adapt.apply_readiness(
            days,
            tue,
            "UPGRADE",
            tsb=-15,
            hard_yesterday=False,
            ctx=ctx,
            phase="build",
            progress=0.0,
            progress_step=0.2,
        )
        == []
    )
    out = adapt.apply_readiness(
        days,
        tue,
        "UPGRADE",
        tsb=0,
        hard_yesterday=False,
        ctx=ctx,
        phase="build",
        progress=0.0,
        progress_step=0.2,
    )
    assert out and days[1].params != before and days[1].is_hard


def test_yesterday_missed_and_overdone(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    missed = days[1]  # Tue HIT
    wed = days[2].date
    rest_days = [d for d in days if d.date >= wed]
    out = adapt.apply_yesterday(
        rest_days, adapt.Yesterday(missed, 10.0, False), wed, ctx, "build", 0.0
    )
    # Wed is 1 day before Thu HIT -> not allowed; nothing else within 72 h keeps 48 h -> drop.
    assert out and out[0].key.startswith(("plan.adapt.reslot", "plan.adapt.drop"))
    hard = [d.date for d in rest_days if d.is_hard]
    assert all((b - a).days >= 2 for a, b in itertools.pairwise(hard))
    over = adapt.apply_yesterday(
        rest_days, adapt.Yesterday(missed, missed.tss * 1.5, True), wed, ctx, "build", 0.0
    )
    assert over and over[-1].key == f"plan.adapt.overdone.{wed}"
