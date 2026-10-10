"""build_trends + run_readiness against a migrated DB, and the trends/readiness/explain CLI."""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from cyp.analysis.longitudinal.run import build_trends, daily_loads, load_latest_report
from cyp.analysis.readiness_job import run_readiness
from cyp.analysis.run import analyze_pending
from cyp.cli import app
from cyp.store.models import (
    Activity,
    Athlete,
    FitnessDaily,
    IcuEvent,
    PowerCurveSnapshot,
    ReadinessDaily,
    WellnessDaily,
)
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.streams import StreamStore
from tests.analysis.conftest import make_ride

AS_OF = dt.date(2026, 10, 2)
START = AS_OF - dt.timedelta(days=69)
K42, K7 = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)


def _interval_ride(n: int, hard_w: float, reps: int, rep_s: int) -> np.ndarray:
    w = np.full(n, 170.0)
    for i in range(reps):
        s = 900 + i * 2 * rep_s
        w[s : s + rep_s] = hard_w
    return w


def _seed(factory: sessionmaker[Session], store: StreamStore) -> None:
    with factory() as s:
        s.add(Athlete(id=1, intervals_id="i1", name="t", weight_kg=64.0, timezone="Asia/Taipei"))
        s.flush()
        AthleteSettingsRepo(s).append_if_changed(
            1,
            dt.date(2026, 1, 1),
            {
                "ftp": 250.0,
                "lthr": 165,
                "max_hr": 190,
                "resting_hr": 47,
                "weight_kg": 64.0,
                "power_zones": None,
                "hr_zones": None,
            },
            source="icu_sport_settings",
        )
        rides = [
            (AS_OF - dt.timedelta(days=1), _interval_ride(5400, 330.0, 5, 300), 110.0),
            (AS_OF - dt.timedelta(days=8), _interval_ride(5400, 300.0, 2, 1200), 105.0),
            (AS_OF - dt.timedelta(days=15), np.full(4 * 3600, 175.0), 160.0),
            (AS_OF - dt.timedelta(days=22), _interval_ride(3600, 380.0, 6, 120), 80.0),
        ]
        totals: dict[dt.date, float] = {d: load for d, _, load in rides}
        for i in range(70):
            day = START + dt.timedelta(days=i)
            if day.weekday() in (2, 4):  # strength sessions count in the PMC too
                load = 30.0 + 5 * (i % 3)
                totals[day] = totals.get(day, 0.0) + load
                s.add(
                    Activity(
                        athlete_id=1,
                        sport_type="WeightTraining",
                        is_ride=False,
                        start_utc=f"{day.isoformat()}T01:00:00Z",
                        start_local=f"{day.isoformat()}T09:00:00",
                        tz="Asia/Taipei",
                        icu_training_load=load,
                    )
                )
        ctl, atl = 40.0, 45.0
        for i in range(70):
            day = START + dt.timedelta(days=i)
            if i:
                load = totals.get(day, 0.0)
                ctl += (load - ctl) * K42
                atl += (load - atl) * K7
            sign = 1 if i % 2 else -1
            s.add(
                WellnessDaily(
                    athlete_id=1,
                    date_local=day,
                    ctl=round(ctl, 2),
                    atl=round(atl, 2),
                    hrv=60 + 3 * sign,
                    resting_hr=47 + sign,
                    sleep_s=7 * 3600 + 900 * sign,
                    sleep_score=80 + 4 * sign,
                    raw_json={"sportInfo": [{"type": "Ride", "eftp": 266.0 if i >= 50 else 250.0}]},
                )
            )
        # Rides carry icu_training_load so the replay uses icu's number, as in production.
        for idx, (day, watts, load) in enumerate(rides):
            lat = 25.1 + (0.0 if idx < 3 else 0.5)
            alt = np.concatenate(
                [
                    np.full(600, 100.0),
                    np.linspace(100, 400, 1500),
                    np.full(len(watts) - 2100, 400.0),
                ]
            )
            a = Activity(
                athlete_id=1,
                sport_type="Ride",
                is_ride=True,
                intervals_id=f"i{idx}",
                start_utc=f"{day.isoformat()}T00:00:00Z",
                start_local=f"{day.isoformat()}T08:00:00",
                tz="Asia/Taipei",
                moving_s=len(watts),
                elapsed_s=len(watts),
                has_power=True,
                has_hr=True,
                kj=float(watts.sum() / 1000),
                icu_training_load=load,
                raw_intervals_json={"icu_power_zones": [55, 75, 90, 105, 120, 150, 999]},
            )
            s.add(a)
            s.flush()
            hr = 120 + (watts - 150) * 0.15
            store.write(
                a.id, make_ride(len(watts), watts=watts, hr=hr, alt=alt, latlng=(lat, 121.5))
            )
        s.add(
            IcuEvent(
                id=9001,
                category="SICK",
                start_date_local="2026-09-25T00:00:00",
                end_date_local="2026-09-26T00:00:00",
                name="flu",
            )
        )
        s.commit()


@pytest.fixture
def store(data_dir: Path) -> StreamStore:
    return StreamStore(data_dir / "streams")


def test_build_trends_persists_everything(
    factory: sessionmaker[Session], store: StreamStore, data_dir: Path
) -> None:
    _seed(factory, store)
    analyze_pending(factory, store=store)
    report = build_trends(
        factory, store, as_of=AS_OF, phase="base", reports_dir=data_dir / "reports"
    )

    # PMC replay tracks the synthetic icu series (built with the same daily totals).
    assert report.pmc_agreement is not None
    assert report.pmc_agreement["within_tolerance"], report.pmc_agreement
    assert report.pmc_agreement["decay"] == "exp"
    with factory() as s:
        _, detail = daily_loads(s)
        rows = s.scalars(select(FitnessDaily).order_by(FitnessDaily.date_local)).all()
        assert rows[0].date_local == START and rows[-1].date_local == AS_OF
        assert rows[-1].ctl_sim is not None and rows[-1].acwr_7_28 is not None
        assert rows[-1].ctl_icu is None  # icu columns are the sync's, untouched here
        snaps = s.scalars(select(PowerCurveSnapshot).where(PowerCurveSnapshot.source == "cyp"))
        assert {p.window for p in snaps} == {"42d", "90d"}
    assert {d.source for d in detail[AS_OF - dt.timedelta(days=1)]} == {"icu"}

    # Power-duration: 5x5 @ 330 and 2x20 @ 300 make a valid 2p fit.
    fit = report.cp_fits["42d"]["cp_2p"]
    assert 280 < fit["cp"] < 320
    # icu eFTP 266 vs FTP 250 (+6.4 %) for 20 days -> proposal, never applied.
    fp = report.ftp_proposal
    assert fp is not None and fp["proposed_ftp"] == 266.0 and fp["days_sustained"] == 20
    with factory() as s:
        assert AthleteSettingsRepo(s).latest(1).ftp == 250.0  # type: ignore[union-attr]

    # Durability: the 4 h steady ride passes 1000 kJ.
    assert any(b["n_long"] for b in report.durability_blocks)
    assert report.tid_weeks and report.tid_weeks[-1]["week_start"] == dt.date(2026, 9, 28)
    assert report.climbs and report.climbs[0]["n"] == 3
    assert report.find("ftp.proposal") is not None

    latest = load_latest_report(data_dir / "reports")
    assert latest is not None and latest["as_of"] == "2026-10-02"
    assert (data_dir / "reports" / "trends" / "2026-10-02.json").is_file()


def test_run_readiness_uses_store_inputs(
    factory: sessionmaker[Session], store: StreamStore
) -> None:
    _seed(factory, store)
    analyze_pending(factory, store=store)
    build_trends(factory, store, as_of=AS_OF)
    sick, today = run_readiness(factory, [dt.date(2026, 9, 25), AS_OF])
    assert (sick.recommendation, sick.status) == ("REST", "SICK")
    assert today.inputs["tsb"] is not None
    assert "ride" in today.inputs["components"] or "ride" in today.inputs["missing"]
    with factory() as s:
        row = s.get(ReadinessDaily, (1, AS_OF))
        assert row is not None and row.explanation["key"] == "readiness.2026-10-02"
        assert row.algo_version == "readiness_v1"


def test_cli_trends_readiness_explain(data_dir: Path, db_url: str, store: StreamStore) -> None:
    from cyp.store.db import make_engine, session_factory
    from cyp.store.migrate import upgrade_head

    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    upgrade_head(db_url)
    engine = make_engine(db_url)
    _seed(session_factory(engine), store)
    engine.dispose()
    runner = CliRunner()
    res = runner.invoke(app, ["analyze", "--rides-only"], env=env)
    assert res.exit_code == 0, res.output
    assert "trends as of" not in res.output

    res = runner.invoke(app, ["trends", "--date", "2026-10-02"], env=env)
    assert res.exit_code == 0, res.output
    assert "PMC vs icu (exp)" in res.output and "[OK]" in res.output
    assert "FTP proposal: 250 -> 266 W" in res.output and "NOT applied" in res.output

    res = runner.invoke(
        app, ["readiness", "--date", "2026-10-02", "--days", "2", "--coverage"], env=env
    )
    assert res.exit_code == 0, res.output
    assert "wellness coverage" in res.output and "readiness 2026-10-01" in res.output

    res = runner.invoke(app, ["explain", "ftp.proposal"], env=env)
    assert res.exit_code == 0, res.output
    assert "reports/trends/latest.json" in res.output
    blob = json.loads(res.output.split("\n", 1)[1])
    assert blob["key"] == "ftp.proposal"

    res = runner.invoke(app, ["explain", "readiness.2026-10-02"], env=env)
    assert res.exit_code == 0 and "readiness_daily" in res.output

    bad = runner.invoke(app, ["trends", "--date", "02-10-2026"], env=env)
    assert bad.exit_code == 2


def test_daily_and_weekly_reports(data_dir: Path, db_url: str, store: StreamStore) -> None:
    from cyp.store.db import make_engine, session_factory
    from cyp.store.migrate import upgrade_head

    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    upgrade_head(db_url)
    engine = make_engine(db_url)
    _seed(session_factory(engine), store)
    engine.dispose()
    runner = CliRunner()
    assert runner.invoke(app, ["analyze", "--rides-only"], env=env).exit_code == 0
    assert runner.invoke(app, ["trends", "--date", "2026-10-02"], env=env).exit_code == 0
    assert (
        runner.invoke(app, ["readiness", "--date", "2026-10-02", "--days", "7"], env=env).exit_code
        == 0
    )

    res = runner.invoke(app, ["report", "daily", "--date", "2026-10-02"], env=env)
    assert res.exit_code == 0, res.output
    md = (data_dir / "reports" / "daily" / "2026-10-02.md").read_text(encoding="utf-8")
    assert md.startswith("# 每日報告 · 2026-10-02（週五）")
    assert "## 1. 今天的判斷" in md and "**準備度" in md and "| HRV |" in md
    assert "**為什麼**" in md and "cyp explain readiness.2026-10-02" in md
    assert "## 3. 昨天（2026-10-01）的訓練" in md and "NP " in md
    assert "最大攝氧間歇" in md or "閾值課" in md or "混合強度" in md
    assert "## 5. FTP 提案" in md and "266" in md
    facts = json.loads((data_dir / "reports" / "daily" / "2026-10-02.json").read_text())
    assert facts["readiness"]["recommendation"] in {"REST", "EASY", "AS_PLANNED", "UPGRADE"}

    res = runner.invoke(app, ["report", "weekly", "--date", "2026-09-27"], env=env)
    assert res.exit_code == 0, res.output
    wk = (data_dir / "reports" / "weekly" / "2026-W39.md").read_text(encoding="utf-8")
    assert "# 週報 · 2026 第 39 週（2026-09-21 – 2026-09-27）" in wk
    assert "## 2. 強度分布（TID）" in wk and "## 3. FTP 證據" in wk
    assert "兩參數 CP 模型" in wk and "## 5. 限制因子與下週方向" in wk
    assert "| 09-25 週五 |" in wk
    assert "休息" in wk  # SICK day verdict shows in the readiness column


def test_cli_daily_pipeline_without_sync(data_dir: Path, db_url: str, store: StreamStore) -> None:
    from cyp.store.db import make_engine, session_factory
    from cyp.store.migrate import upgrade_head

    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    upgrade_head(db_url)
    engine = make_engine(db_url)
    _seed(session_factory(engine), store)
    engine.dispose()
    res = CliRunner().invoke(app, ["daily", "--no-sync"], env=env)
    assert res.exit_code == 0, res.output
    assert "analyze:" in res.output and "readiness " in res.output
    assert "daily report:" in res.output
    weekly = CliRunner().invoke(app, ["weekly", "--no-sync"], env=env)
    assert weekly.exit_code == 0, weekly.output and "weekly report:" in weekly.output
