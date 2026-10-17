"""Plan compliance (services/compliance.py) and the read-only ``cyp plan replay``.

Spec adaptive-planning, Phase 0b: the morning run fills ``planned_workouts.compliance`` for the
days before today; the replay re-runs readiness and the planner day by day and writes nothing.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from cyp.cli import app
from cyp.dataset import CACHE, data_version
from cyp.planning.compliance import COMPLIANCE_VERSION, compliance_record
from cyp.services import compliance as compliance_service
from cyp.services import plan as plan_service
from cyp.services import plan_replay
from cyp.services.context import AppContext
from cyp.store.db import make_readonly_engine, session_factory
from cyp.store.models import PlannedWorkout, ReadinessDaily
from tests.api.conftest import synthetic_athlete_config

#: The synthetic rides end 2026-10-05; the tests move the season start back so plans overlap them.
SEASON_START = dt.date(2026, 9, 21)
SEASON_DAY = dt.date(2026, 9, 29)
runner = CliRunner()


def _early_season(ctx: AppContext) -> None:
    path = Path(ctx.athlete_config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["season"]["start"] = SEASON_START
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ------------------------------------------------------------------------------- record


def test_record_fields_and_overdone_both_sides() -> None:
    base = {"role": "endurance", "intent": "endurance", "intent_verdict": "achieved"}
    ok = compliance_record(
        **base, planned_tss=60, reference_tss=65, actual_tss=90, rode=True, rpe=4,
        rated_class="endurance", rated_minutes=90,
    )  # fmt: skip
    assert ok["algo_version"] == COMPLIANCE_VERSION
    assert ok["ratio"] == 1.5 and ok["reference_tss"] == 65 and not ok["overdone"]
    assert ok["rpe_delta"] == 1.0 and ok["rated_class"] == "endurance" and not ok["skipped"]
    # Over both the factor (1.3 x 65) and the margin (65 + 40).
    over = compliance_record(**base, planned_tss=60, reference_tss=65, actual_tss=106, rode=True)
    assert over["overdone"] and over["rpe_delta"] is None
    # A ride on a day planned as a short spin is judged against the slot's own target.
    spin = compliance_record(**base, planned_tss=19, reference_tss=88, actual_tss=74, rode=True)
    assert not spin["overdone"]


def test_record_skipped_and_rest() -> None:
    skipped = compliance_record(
        role="hit", intent="vo2", planned_tss=70, reference_tss=None, actual_tss=12.0,
        rode=False, intent_verdict="achieved",
    )  # fmt: skip
    assert skipped["skipped"] and skipped["actual_tss"] is None
    assert skipped["intent_verdict"] is None and skipped["ratio"] is None
    rest = compliance_record(
        role="rest", intent="rest", planned_tss=0, reference_tss=0, actual_tss=0, rode=False,
        intent_verdict=None,
    )  # fmt: skip
    assert not rest["skipped"] and not rest["overdone"]


# ------------------------------------------------------------------------------ service


def _plan_season_day(ctx: AppContext) -> None:
    _early_season(ctx)
    ctx.fixed_now = dt.datetime.combine(SEASON_DAY, dt.time(6, 0))
    plan_service.commit(ctx, today=SEASON_DAY)


def test_morning_fills_yesterday_and_dataset_sees_it(ctx: AppContext) -> None:
    _plan_season_day(ctx)
    with ctx.factory() as s:
        row = s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.date_local == SEASON_DAY)
        ).first()
        assert row is not None and row.compliance is None
        assert (row.steps or {}).get("reference_tss") is not None
        before = data_version(s)
    changed = compliance_service.record(ctx, SEASON_DAY + dt.timedelta(days=1))
    assert changed >= 1
    with ctx.factory() as s:
        row = s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.date_local == SEASON_DAY)
        ).first()
        assert row is not None and row.compliance is not None
        rec = row.compliance
        assert rec["algo_version"] == COMPLIANCE_VERSION
        assert rec["planned_tss"] == pytest.approx(row.target_tss, abs=0.1)
        assert set(rec) >= {
            "actual_tss", "ratio", "reference_tss", "intent_verdict", "overdone", "rpe",
            "feel", "rpe_delta", "skipped",
        }  # fmt: skip
        # Future days are never judged.
        later = s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.date_local > SEASON_DAY)
        ).all()
        assert later and all(r.compliance is None for r in later)
        assert data_version(s) != before
    # Idempotent: the same morning again changes nothing.
    assert compliance_service.record(ctx, SEASON_DAY + dt.timedelta(days=1)) == 0
    ds = ctx.dataset()
    assert ds.planned[SEASON_DAY][0].compliance == rec


# ------------------------------------------------------------------------------- replay


def _counts(ctx: AppContext) -> tuple[int, int, str]:
    with ctx.factory() as s:
        return (
            s.scalar(select(func.count(PlannedWorkout.id))) or 0,
            s.scalar(select(func.count(ReadinessDaily.date_local))) or 0,
            data_version(s),
        )


def test_replay_writes_nothing_and_reports_weeks(ctx: AppContext) -> None:
    _early_season(ctx)
    before = _counts(ctx)
    start = dt.date(2026, 9, 28)
    result = plan_replay.replay(ctx, start, start + dt.timedelta(days=13))
    assert _counts(ctx) == before
    assert len(result.days) == 14 and [w.start for w in result.weeks] == [
        start,
        start + dt.timedelta(days=7),
    ]
    assert all(d.readiness is not None for d in result.days)
    judged = [d for d in result.days if d.compliance is not None]
    assert any(d.rode for d in judged) and all(d.template_id for d in judged)
    # The seeded rides end 2026-10-05: planned days after it are skipped.
    assert all(d.skipped for d in judged if d.date > dt.date(2026, 10, 5))
    blob = result.to_dict()
    assert blob["totals"]["days"] == 14 and blob["weeks"][0]["target_tss"] > 0
    json.dumps(blob)  # JSON-ready


def test_readonly_engine_refuses_writes(data: Path) -> None:
    engine = make_readonly_engine(f"sqlite:///{data / 'cyp.sqlite'}")
    with session_factory(engine)() as s, pytest.raises(OperationalError):
        s.execute(PlannedWorkout.__table__.delete())
        s.commit()
    engine.dispose()
    with pytest.raises(ValueError):
        make_readonly_engine(f"sqlite:///{data / 'missing.sqlite'}")


def test_cli_replay_json_and_text(data: Path) -> None:
    CACHE.clear()
    env = {"CYP_DATA_DIR": str(data), "CYP_DB_URL": f"sqlite:///{data / 'cyp.sqlite'}"}
    cfg = ["--athlete-config", str(synthetic_athlete_config(data))]
    span = ["--from", "2026-10-05", "--to", "2026-10-08"]
    res = runner.invoke(app, ["plan", "replay", *span, "--json", *cfg], env=env)
    assert res.exit_code == 0, res.output
    blob = json.loads(res.stdout)
    assert blob["start"] == "2026-10-05" and len(blob["days"]) == 4
    text = runner.invoke(app, ["plan", "replay", *span, *cfg], env=env)
    assert text.exit_code == 0, text.output
    assert "read-only" in text.output and "total:" in text.output
    assert not (data / "logs").exists() or not any((data / "logs").iterdir())
    bad = runner.invoke(app, ["plan", "replay", "--from", "2026-10-08", "--to", "2026-10-05"])
    assert bad.exit_code == 2


def test_replay_reports_gate_decisions_only_with_the_flag(ctx: AppContext) -> None:
    _early_season(ctx)
    start, end = dt.date(2026, 9, 21), dt.date(2026, 10, 4)
    off = plan_replay.replay(ctx, start, end)
    assert all(w.gate is None for w in off.weeks)
    ctx.settings = ctx.settings.model_copy(
        update={"cyp_features": "notify.macos=off,plan.progression_gate=on"}
    )
    on = plan_replay.replay(ctx, start, end)
    assert on.weeks[0].gate is None  # season week 1: nothing to judge yet
    g = on.weeks[1].gate
    assert g is not None and g["decision"] in {"up", "hold", "down", "early_recovery",
                                               "back_from_break", "extra"}  # fmt: skip
    assert g["progression_index"] <= g["season_index"] + g["extras_used"]
    assert on.weeks[1].planned_first_morning is not None


def test_casual_ride_makes_the_slot_casual_and_not_overdone(ctx: AppContext) -> None:
    """Spec casual-and-strava: the carrying ride tagged casual -> ``casual``, never overdone."""
    from cyp.services import adaptive
    from cyp.services.ride_feedback import set_casual

    _plan_season_day(ctx)
    today = SEASON_DAY + dt.timedelta(days=7)
    plain = compliance_service.compute(ctx, today)
    assert plain and all(rec["casual"] is False for rec in plain.values())
    with ctx.factory() as s:
        days = {pw.id: pw.date_local for pw in s.scalars(select(PlannedWorkout))}
    ds = ctx.dataset()
    ridden = [i for i, rec in plain.items() if rec["actual_tss"] is not None]
    assert ridden, "the seeded week has ridden planned days"
    target = ridden[0]
    day = days[target]
    for a in ds.on(day):
        if a.is_ride:
            set_casual(ctx, a.id, True)
    casual = compliance_service.compute(ctx, today)
    rec = casual[target]
    assert rec["casual"] is True and rec["overdone"] is False
    assert rec["intent_verdict"] in ("casual", None)
    assert rec["actual_tss"] == plain[target]["actual_tss"]  # the load still counts
    others = [i for i in ridden if days[i] != day]
    assert all(casual[i] == plain[i] for i in others)
    assert day in adaptive.plan_inputs(ctx, today).casual_days
