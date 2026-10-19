"""Age / health rules and the distance-goal long-ride progression (docs/specs, ADR-0008 flags)."""

from __future__ import annotations

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest
import yaml

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.planning.athlete_rules import effective
from cyp.planning.guardrails import check
from cyp.planning.planner import DayPlan, PlanContext, availability, plan_week
from cyp.planning.season import build_skeleton, week_targets
from cyp.planning.templates import Template
from cyp.settings import AthleteConfig
from tests.planning.test_season_planner import _gi

REPO = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 7)


def _cfg(**changes: Any) -> AthleteConfig:
    """Reference athlete with dotted-path changes (value None removes the key)."""
    data = yaml.safe_load((REPO / "tests/fixtures/athlete.reference.yaml").read_text("utf-8"))
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        node = data
        for p in parents:
            node = node.setdefault(p, {})
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return AthleteConfig.model_validate(data)


def test_no_new_fields_means_no_change(cfg: AthleteConfig) -> None:
    out, notes = effective(cfg, TODAY)
    assert out is cfg and notes == []
    assert all(
        w.long_ride_minutes is None and not w.over_distance for w in build_skeleton(cfg).weeks
    )


def test_age_defaults_fill_only_unset_values() -> None:
    implicit = _cfg(athlete__birth_year=1950, season__load_pattern=None, planner__ramp_cap=None)
    out, notes = effective(implicit, TODAY)
    assert out.season.load_pattern == "2:1" and out.season.tests.ramp is not None  # tests explicit
    assert out.planner.ramp_cap == {"base": 4.0, "build": 4.0, "threshold": 3.0}
    assert notes and "76 歲" in notes[0]
    sk = build_skeleton(out)
    assert all(w.hit_sessions <= 2 for w in sk.weeks)
    base = [w.week_in_block for w in sk.block_weeks(0) if w.recovery]
    assert base == [3, 6]  # 2:1 (3:1 would be [4, 8])

    explicit = _cfg(athlete__birth_year=1950)
    kept, _ = effective(explicit, TODAY)
    assert kept.season.load_pattern == "3:1"  # written in athlete.yaml: the athlete's choice wins

    young, notes_young = effective(_cfg(athlete__birth_year=1990), TODAY)
    assert notes_young == [] and young.season.load_pattern == "3:1"


def test_health_flags_are_hard_caps() -> None:
    out, notes = effective(_cfg(athlete__health_flags=["heart_or_bp"]), TODAY)
    assert all(v <= 1 for v in out.planner.hit_per_week.values())
    assert out.season.final_test is False
    assert out.season.tests.ramp is None and out.season.tests.twenty_min is None
    assert any("醫生" in n for n in notes)
    assert all(w.test is None and w.hit_sessions <= 1 for w in build_skeleton(out).weeks)
    screened, notes_ok = effective(_cfg(athlete__health_flags=[]), TODAY)
    assert notes_ok == [] and screened.season.final_test is True


def test_flag_off_disables_rules_and_progression() -> None:
    cfg = _distance_cfg(athlete__health_flags=["injury"])
    out, notes = effective(cfg, TODAY, athlete_rules=False, long_ride_progression=False)
    assert notes == [] and out.availability.long_ride_max_minutes is None
    assert all(w.long_ride_minutes is None for w in build_skeleton(out).weeks)


def _distance_cfg(**more: Any) -> AthleteConfig:
    base = _cfg(**{"availability__long_ride_max_minutes": 300, "availability__sat": 210, **more})
    goals = [g.model_dump() for g in base.goals] + [
        {
            "name": "160 km",
            "date": "2027-04-02",
            "category": "TARGET",
            "kind": "distance",
            "target": {"km": 160, "kmh": 24},
        }
    ]
    return AthleteConfig.model_validate({**base.model_dump(), "goals": goals})


def test_long_ride_progression_shape() -> None:
    sk = build_skeleton(_distance_cfg())
    longs = [w.long_ride_minutes for w in sk.weeks if w.phase != "test" and not w.recovery]
    over = [w for w in sk.weeks if w.over_distance]
    blocks = {w.block_idx for w in sk.weeks if w.phase != "test"}
    assert len(over) == len(blocks)  # exactly one over-distance ride per block
    assert all(w.long_ride_minutes and w.long_ride_minutes <= 360 for w in over)
    regular = [
        m
        for w, m in zip(
            [w for w in sk.weeks if w.phase != "test" and not w.recovery], longs, strict=True
        )
        if not w.over_distance
    ]
    assert regular == sorted(regular) and max(regular) == 300 and regular[0] == 210
    longest = 210
    for w in sk.weeks:
        if w.long_ride_minutes is None:
            continue
        if w.over_distance:
            assert w.long_ride_minutes <= longest + 60
        longest = max(longest, w.long_ride_minutes)
    assert all(w.long_ride_minutes == 147 for w in sk.weeks if w.recovery)
    assert all(w.long_ride_minutes is None for w in sk.weeks if w.phase == "test")


def test_over_distance_week_plans_the_distance_template(library: dict[str, Template]) -> None:
    cfg = _distance_cfg()
    sk = build_skeleton(cfg)
    ctx = PlanContext(cfg=cfg, library=library, goal_date=sk.goal_date)
    w = next(w for w in sk.weeks if w.over_distance)
    assert availability(w, ctx)[w.start + dt.timedelta(days=5)] == w.long_ride_minutes
    days = plan_week(w, week_targets(w, 60.0, weekly_max_minutes=1200, prev_loading_tss=650.0), ctx)
    sat = next(d for d in days if d.role == "long_ride")
    assert sat.template_id == "long_ride_distance"
    assert sat.workout is not None and sat.workout.duration_s / 60 <= w.long_ride_minutes


def test_distance_season_is_guardrail_clean(library: dict[str, Template]) -> None:
    cfg = _distance_cfg(athlete__birth_year=1950, season__load_pattern=None)
    cfg, _ = effective(cfg, TODAY)
    sk = build_skeleton(cfg)
    ctx = PlanContext(cfg=cfg, library=library, goal_date=sk.goal_date)
    ctl = atl = 55.0
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
    assert check(days, _gi(cfg, PMCState(sk.start - dt.timedelta(days=1), 55.0, 55.0))) == []
    hard = [d.date for d in days if d.is_hard]
    assert all((b - a).days >= 2 for a, b in itertools.pairwise(hard))
    assert any(d.template_id == "long_ride_distance" for d in days)


@pytest.mark.parametrize("year", [None, 1990])
def test_age_none_or_young_keeps_reference(year: int | None) -> None:
    cfg = _cfg(athlete__birth_year=year) if year else _cfg()
    assert effective(cfg, TODAY)[1] == []
