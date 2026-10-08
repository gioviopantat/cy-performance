"""build_plan against a migrated DB (persistence, idempotency, adaptation) and `cyp plan`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from cyp.cli import app
from cyp.planning.job import build_plan
from cyp.planning.renderer import render
from cyp.publish.plan_events import event_specs
from cyp.settings import AthleteConfig
from cyp.store.models import (
    Athlete,
    FitnessDaily,
    IcuEvent,
    PlannedWorkout,
    PlanRevision,
    ReadinessDaily,
    Season,
    WeekPlan,
)

TODAY = dt.date(2026, 10, 6)  # season week 1, Tuesday
NOW = dt.datetime(2026, 10, 6, 7, 0)


def _seed(factory: sessionmaker[Session], *, readiness: str | None = None) -> None:
    with factory() as s:
        s.add(Athlete(id=1, intervals_id="i1", name="t", weight_kg=64.0))
        for i in range(60):
            d = TODAY - dt.timedelta(days=60 - i)
            s.add(
                FitnessDaily(athlete_id=1, date_local=d, ctl_icu=50.0, atl_icu=52.0, tsb_icu=-2.0)
            )
        if readiness:
            s.add(
                ReadinessDaily(
                    athlete_id=1,
                    date_local=TODAY,
                    score_0_100=20.0,
                    status="OVERREACHED",
                    recommendation=readiness,
                )
            )
        s.add(
            IcuEvent(
                id=77, category="RACE_C", start_date_local="2026-10-10T00:00:00", name="社區賽"
            )
        )
        s.add(
            IcuEvent(
                id=78,
                category="NOTE",
                start_date_local="2026-10-08T00:00:00",
                training_availability="LIMITED",
                max_training_time=45 * 60,
                name="加班",
            )
        )
        s.commit()


def test_build_plan_persists_and_is_idempotent(
    factory: sessionmaker[Session], cfg: AthleteConfig
) -> None:
    _seed(factory)
    run = build_plan(factory, cfg, today=TODAY, now_local=NOW)
    assert run.days[0].date == TODAY and run.days[-1].date == TODAY + dt.timedelta(days=13)
    assert not run.needs_review, run.violations
    by_date = {d.date: d for d in run.days}
    assert by_date[dt.date(2026, 10, 10)].workout is None  # athlete's race wins
    assert by_date[dt.date(2026, 10, 8)].minutes <= 45  # LIMITED note caps Thursday
    assert any("RACE_C" in e.headline_zh for e in run.adaptations)
    with factory() as s:
        rows = s.scalars(select(PlannedWorkout).order_by(PlannedWorkout.date_local)).all()
        assert rows and all(r.status == "proposed" for r in rows)
        assert rows[0].external_id == f"cyp:s20261005:{rows[0].date_local}:1"
        assert rows[0].explanation["key"] == f"plan.day.{rows[0].date_local}"
        assert s.scalars(select(Season)).one().config["key"] == "s20261005"
        assert len(s.scalars(select(WeekPlan)).all()) == 3
        n_rows = len(rows)
    assert run.changes["created"] and not run.changes["changed"]

    again = build_plan(factory, cfg, today=TODAY, now_local=NOW)
    assert again.changes == {"created": [], "changed": [], "removed": []}
    with factory() as s:
        assert len(s.scalars(select(PlannedWorkout)).all()) == n_rows
        assert len(s.scalars(select(PlanRevision)).all()) == 2

    specs = event_specs(again, render=render)
    assert all(sp.external_id.startswith("cyp:s20261005:") for sp in specs)
    assert specs[0].description.splitlines()[-3].startswith("# ")
    assert all(sp.target_load and sp.target_load > 0 for sp in specs)


def test_readiness_rest_and_cutoff(factory: sessionmaker[Session], cfg: AthleteConfig) -> None:
    _seed(factory, readiness="REST")
    run = build_plan(factory, cfg, today=TODAY, now_local=NOW, persist=False)
    assert run.days[0].workout is None
    assert any(e.key == f"plan.adapt.readiness.{TODAY}" for e in run.adaptations)
    late = build_plan(factory, cfg, today=TODAY, now_local=NOW.replace(hour=11), persist=False)
    # After the 10:00 cut-off today is frozen: readiness no longer rewrites it.
    assert late.days[0].workout is not None


def test_before_season_plans_from_season_start(
    factory: sessionmaker[Session], cfg: AthleteConfig
) -> None:
    _seed(factory)
    run = build_plan(
        factory,
        cfg,
        today=dt.date(2026, 10, 2),
        now_local=dt.datetime(2026, 10, 2, 8),
        persist=False,
    )
    assert run.days[0].date == dt.date(2026, 10, 5)
    assert run.days[-1].date == dt.date(2026, 10, 15)


def test_cli_plan_dry_run_and_guards(data_dir: Path, db_url: str, athlete_yaml: Path) -> None:
    from cyp.store.db import make_engine, session_factory
    from cyp.store.migrate import upgrade_head

    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    upgrade_head(db_url)
    engine = make_engine(db_url)
    _seed(session_factory(engine))
    engine.dispose()
    runner = CliRunner()
    cfg_opt = ["--athlete-config", str(athlete_yaml)]
    missing = runner.invoke(app, ["plan", "--dry-run"], env=env)
    assert missing.exit_code == 2  # tests run outside the repo: no config/athlete.yaml here
    res = runner.invoke(app, ["plan", "--date", "2026-10-06", "--dry-run", *cfg_opt], env=env)
    assert res.exit_code == 0, res.output
    assert "plan 2026-10-06 (season s20261005, mode propose)" in res.output
    assert "第 1 週 基礎期" in res.output and "甜蜜點" in res.output
    no_confirm = runner.invoke(app, ["plan", "--apply", *cfg_opt], env=env)
    assert no_confirm.exit_code == 2 and "--confirm-write" in no_confirm.output
    no_key = runner.invoke(app, ["plan", "--date", "2026-10-06", "--publish", *cfg_opt], env=env)
    assert no_key.exit_code == 2 and "INTERVALS_API_KEY" in no_key.output


def test_guardrail_rules_have_plain_chinese_names() -> None:
    from cyp.planning.guardrails import RULE_ZH

    expected = {
        "tsb_floor",
        "ramp_rate",
        "hit_spacing",
        "hit_per_week",
        "weekly_tss_vs_mean",
        "rest_days",
        "single_ride",
    }
    assert set(RULE_ZH) == expected
    assert all("_" not in label for label in RULE_ZH.values())


def test_moving_season_start_supersedes_old_proposals(
    factory: sessionmaker[Session], cfg: AthleteConfig
) -> None:
    """Regression 2026-10-07: a new season key left the old key's proposals active (duplicates)."""
    _seed(factory)
    later = cfg.model_copy(
        update={
            "season": cfg.season.model_copy(
                update={"start": cfg.season.start + dt.timedelta(weeks=1)}
            )
        }
    )
    build_plan(factory, later, today=TODAY, now_local=NOW)
    run = build_plan(factory, cfg, today=TODAY, now_local=NOW)
    assert run.changes["removed"], "the other season's proposals must be superseded"
    with factory() as s:
        active = s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.status.in_(("proposed", "published")))
        ).all()
    dates = [w.date_local for w in active]
    assert len(dates) == len(set(dates)), "one active proposal per day"
    assert all(w.external_id.startswith(f"cyp:{run.season_key}:") for w in active)
