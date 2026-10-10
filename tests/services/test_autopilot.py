"""Autopilot stages, flags, write policy and the job_runs row (services/autopilot.py)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from cyp.ingest.intervals.sync import CURSOR_ATHLETE_ID
from cyp.ingest.intervals.sync import SOURCE as ICU_SOURCE
from cyp.profiles import set_planner_mode
from cyp.services.autopilot import run_autopilot
from cyp.services.context import AppContext
from cyp.services.publish import store_upsert_mode
from cyp.store.models import JobRun
from cyp.store.repo.sync_cursors import SyncCursorRepo
from tests.api.conftest import NOW
from tests.services.test_publish_guard import FakeIcu

MONDAY = dt.datetime(2026, 10, 12, 6, 0)


def _prep(ctx: AppContext, *, features: str = "sync.intervals=off", key: bool = True) -> FakeIcu:
    update: dict[str, Any] = {"cyp_features": features}
    if key:
        update["intervals_api_key"] = SecretStr("k")
    ctx.settings = ctx.settings.model_copy(update=update)
    with ctx.factory() as s:
        SyncCursorRepo(s).set(ICU_SOURCE, CURSOR_ATHLETE_ID, "i42")
        s.commit()
    store_upsert_mode(ctx, "upsert")
    return FakeIcu(owner="i42")


def _statuses(report: Any) -> dict[str, str]:
    return {s.name: s.status for s in report.stages}


def test_propose_profile_runs_every_stage_without_writing(ctx: AppContext) -> None:
    cal = _prep(ctx)
    report = run_autopilot(ctx, now_local=NOW, client_factory=lambda k, a: cal)
    assert report.ok, report.to_dict()
    assert _statuses(report) == {
        "sync": "skipped",  # feature off in this test (no network)
        "analyze": "ok",
        "report.daily": "ok",
        "report.weekly": "skipped",  # not a Monday
        "plan": "ok",
        "publish": "ok",
    }
    assert "planner.mode propose" in (report.stage("publish") or _no()).detail
    assert cal.writes == []
    with ctx.factory() as s:
        row = s.scalars(select(JobRun).where(JobRun.job == "autopilot")).one()
    assert row.status == "ok"


def test_apply_profile_writes_and_weekly_on_monday(ctx: AppContext) -> None:
    cal = _prep(ctx)
    set_planner_mode(Path(ctx.athlete_config_path), "apply")
    report = run_autopilot(ctx, now_local=MONDAY, client_factory=lambda k, a: cal)
    st = _statuses(report)
    assert st["report.weekly"] == "ok" and st["publish"] == "ok", report.to_dict()
    assert (report.stage("publish") or _no()).detail.startswith("written")
    assert cal.writes
    cal2 = _prep(ctx)
    again = run_autopilot(
        ctx, now_local=MONDAY, allow_write=False, client_factory=lambda k, a: cal2
    )
    assert "--no-write" in (again.stage("publish") or _no()).detail and cal2.writes == []


def test_failed_stage_is_isolated_and_recorded(ctx: AppContext) -> None:
    cal = _prep(ctx, features="", key=False)  # sync on but no key -> sync fails
    report = run_autopilot(ctx, now_local=NOW, client_factory=lambda k, a: cal)
    st = _statuses(report)
    assert st["sync"] == "failed" and st["plan"] == "ok" and not report.ok
    assert st["publish"] == "failed"  # no key for the calendar either
    with ctx.factory() as s:
        row = s.scalars(select(JobRun).where(JobRun.job == "autopilot")).one()
    assert row.status == "failed" and "sync" in (row.error or "")


def test_flags_switch_stages_off(ctx: AppContext) -> None:
    cal = _prep(ctx, features="sync.intervals=off,plan.horizon=off,report.daily=off")
    report = run_autopilot(ctx, now_local=NOW, client_factory=lambda k, a: cal)
    st = _statuses(report)
    assert st["plan"] == "skipped" and st["publish"] == "skipped"
    assert st["report.daily"] == "skipped" and report.ok


def test_failed_calendar_write_fails_the_run(ctx: AppContext) -> None:
    from tests.services.test_publish_guard import FailingIcu

    _prep(ctx)
    set_planner_mode(Path(ctx.athlete_config_path), "apply")
    cal = FailingIcu(owner="i42")
    report = run_autopilot(ctx, now_local=NOW, client_factory=lambda k, a: cal)
    publish = report.stage("publish") or _no()
    assert publish.status == "failed" and "503" in publish.detail and not report.ok


def test_invalid_config_is_a_recorded_failure(ctx: AppContext) -> None:
    Path(ctx.athlete_config_path).write_text("athlete: {}\n")
    report = run_autopilot(ctx, now_local=NOW)
    assert [s.name for s in report.stages] == ["config"] and not report.ok
    with ctx.factory() as s:
        row = s.scalars(select(JobRun).where(JobRun.job == "autopilot")).one()
    assert row.status == "failed"


def _no() -> Any:
    pytest.fail("stage missing")
