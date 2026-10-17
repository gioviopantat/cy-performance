"""Casual-ride tag in the planner, compliance and gate (docs/specs/casual-and-strava.md)."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from cyp.dataset import ActivityRow, Dataset, PlannedRow
from cyp.planning import adapt
from cyp.planning.compliance import compliance_record
from cyp.planning.gate import week_evidence
from cyp.planning.planner import PlanContext
from cyp.settings import AthleteConfig
from tests.planning.test_season_planner import _week_plan


def test_casual_yesterday_reslots_a_hard_plan_whatever_was_ridden(
    cfg: AthleteConfig, ctx: PlanContext
) -> None:
    days = _week_plan(cfg, ctx, 9)
    hard, today = days[1], days[2].date
    assert hard.is_hard
    rest = [d for d in days if d.date >= today]
    # Ridden hard and at full load: done, nothing moves.
    done = adapt.Yesterday(hard, hard.tss, True)
    assert adapt.apply_yesterday(rest, done, today, ctx, "build", 0.0) == []
    # The same ride tagged casual: the plan counts as not done.
    casual = adapt.Yesterday(hard, hard.tss, True, casual=True)
    out = adapt.apply_yesterday(rest, casual, today, ctx, "build", 0.0)
    assert out and out[0].key.startswith(("plan.adapt.reslot", "plan.adapt.drop"))


def test_casual_yesterday_is_never_overdone(cfg: AthleteConfig, ctx: PlanContext) -> None:
    days = _week_plan(cfg, ctx, 9)
    easy = next(d for d in days if not d.is_hard and d.workout is not None and d.tss > 0)
    later = [d for d in days if d.date > easy.date]
    today = later[0].date
    over = adapt.Yesterday(easy, easy.tss + 150, False, reference_tss=easy.tss)
    out = adapt.apply_yesterday(later, over, today, ctx, "build", 0.0)
    assert out and out[-1].key == f"plan.adapt.overdone.{today}"
    days = _week_plan(cfg, ctx, 9)
    easy = next(d for d in days if not d.is_hard and d.workout is not None and d.tss > 0)
    later = [d for d in days if d.date > easy.date]
    casual = replace(over, planned=easy, casual=True)
    assert adapt.apply_yesterday(later, casual, today, ctx, "build", 0.0) == []


def test_compliance_record_casual_vs_not() -> None:
    common = {
        "role": "endurance",
        "intent": "endurance",
        "planned_tss": 100.0,
        "reference_tss": 100.0,
        "actual_tss": 250.0,
        "rode": True,
    }
    plain = compliance_record(**common, intent_verdict="missed")
    assert plain["overdone"] is True and plain["casual"] is False
    casual = compliance_record(**common, intent_verdict="casual", casual=True)
    assert casual["overdone"] is False and casual["casual"] is True
    assert casual["intent_verdict"] == "casual" and casual["actual_tss"] == 250.0
    # Not ridden: never casual.
    skipped = compliance_record(**{**common, "rode": False}, intent_verdict=None, casual=True)
    assert skipped["casual"] is False and skipped["skipped"] is True


def _ds(compliance: dict[str, object]) -> Dataset:
    mon = dt.date(2026, 10, 5)
    act = ActivityRow(
        id=1,
        date=mon,
        sport="Ride",
        name=None,
        is_ride=True,
        moving_s=3600,
        kj=None,
        has_power=True,
        has_hr=True,
        load=80.0,
        load_source="icu",
    )
    row = PlannedRow(
        date=mon,
        external_id="cyp-x",
        template_id="t",
        params={},
        role="hit",
        indoor=False,
        target_tss=80.0,
        status="live",
        compliance=compliance,
    )
    ds = Dataset(
        version="t", athlete_id=1, weight_kg=None, settings=(), activities=(act,), loads={},
        wellness={}, fitness={}, readiness={}, events=(), icu_models={},
    )  # fmt: skip
    return replace(ds, planned={mon: (row,)})


def test_gate_counts_a_casual_key_session_as_missed() -> None:
    mon = dt.date(2026, 10, 5)
    achieved = week_evidence(_ds({"intent_verdict": "achieved", "skipped": False}), mon, 300.0)
    assert (achieved.key_achieved, achieved.key_missed) == (1, 0)
    casual = week_evidence(
        _ds({"intent_verdict": "casual", "skipped": False, "casual": True}), mon, 300.0
    )
    assert (casual.key_achieved, casual.key_missed) == (0, 1)
