"""analysis/run.py against a migrated SQLite DB + Parquet store, and the cyp analyze CLI."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from cyp.analysis.run import ALGO_VERSION, analyze_activity, analyze_pending, resolve_inputs
from cyp.cli import app
from cyp.core.errors import NotFoundError
from cyp.store.models import Activity, ActivityMetrics, Athlete
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.repo.job_runs import JobRunRepo
from cyp.store.repo.metrics import ActivityMetricsRepo
from cyp.store.streams import StreamStore
from tests.analysis.conftest import make_ride

ICU_ZONES = [55, 75, 90, 105, 120, 150, 999]


def _seed(
    factory: sessionmaker[Session],
    store: StreamStore,
    *,
    with_settings: bool = True,
) -> dict[str, int]:
    """Athlete + settings (FTP 250 from 2026-09-01) + three activities with streams."""
    with factory() as s:
        s.add(Athlete(id=1, name="t", weight_kg=70.0, timezone="Asia/Taipei"))
        s.flush()
        if with_settings:
            AthleteSettingsRepo(s).append_if_changed(
                1,
                dt.date(2026, 9, 1),
                {
                    "ftp": 250.0,
                    "lthr": 160,
                    "max_hr": 185,
                    "resting_hr": 50,
                    "weight_kg": 70.0,
                    "power_zones": None,
                    "hr_zones": None,
                },
                source="icu_sport_settings",
            )
        repo = ActivityRepo(s)
        ids: dict[str, int] = {}
        common = {
            "athlete_id": 1,
            "tz": "Asia/Taipei",
            "moving_s": 3600,
            "elapsed_s": 3600,
            "raw_intervals_json": {
                "icu_power_zones": ICU_ZONES,
                "icu_hr_zones": [131, 146, 153, 163, 167, 172, 181],
            },
        }
        power = repo.upsert(
            {
                **common,
                "intervals_id": "i1",
                "sport_type": "Ride",
                "name": "steady",
                "start_utc": "2026-09-10T00:00:00Z",
                "start_local": "2026-09-10T08:00:00+08:00",
                "has_power": True,
                "has_hr": True,
                "icu_training_load": 62.0,
                "np_w": 199.0,
                "icu_ftp": 250.0,
                "raw_intervals_json": {
                    **common["raw_intervals_json"],
                    "icu_weighted_avg_watts": 199,
                },
            }
        )
        hr_only = repo.upsert(
            {
                **common,
                "intervals_id": "i2",
                "sport_type": "Ride",
                "name": "hr only",
                "start_utc": "2026-08-01T00:00:00Z",  # before the settings row -> icu_ftp fallback
                "start_local": "2026-08-01T08:00:00+08:00",
                "has_power": False,
                "has_hr": True,
                "icu_training_load": 98.0,
                "icu_ftp": 320.0,
            }
        )
        strength = repo.upsert(
            {
                **common,
                "intervals_id": "i3",
                "sport_type": "WeightTraining",
                "name": "gym",
                "start_utc": "2026-09-11T00:00:00Z",
            }
        )
        no_streams = repo.upsert(
            {
                **common,
                "intervals_id": "i4",
                "sport_type": "Ride",
                "name": "no file",
                "start_utc": "2026-09-12T00:00:00Z",
                "has_power": True,
            }
        )
        for row in (power, hr_only, strength, no_streams):
            row.pending_detail = False
            row.pending_streams = False
        ids = {
            "power": power.id,
            "hr_only": hr_only.id,
            "strength": strength.id,
            "no_streams": no_streams.id,
        }
        for key in ("power", "hr_only"):
            repo.upsert_stream_file(
                ids[key], {"source": "intervals", "path": str(store.path_for(ids[key])), "hz": 1.0}
            )
        s.commit()
    store.write(ids["power"], make_ride(3600))
    store.write(ids["hr_only"], make_ride(3600, watts=None, hr=160.0))
    return ids


@pytest.fixture
def store(data_dir: Path) -> StreamStore:
    return StreamStore(data_dir / "streams")


def test_resolve_inputs_precedence(factory: sessionmaker[Session], store: StreamStore) -> None:
    ids = _seed(factory, store)
    with factory() as s:
        power = resolve_inputs(s, s.get(Activity, ids["power"]))  # type: ignore[arg-type]
        assert power.ftp == 250.0 and power.ftp_source.startswith("settings_history:2026-09-01")
        assert power.weight_kg == 70.0 and power.lthr == 160 and power.max_hr == 185
        assert (
            power.zones_source == "raw_intervals_json.icu_power_zones"
        )  # settings row has no zones
        assert power.power_zones is not None and power.power_zones.zones[0].hi == pytest.approx(
            137.5
        )
        assert power.hr_zones is not None and len(power.hr_zones.zones) == 7
        assert power.icu_np_w == 199.0 and power.icu_training_load == 62.0

        hr_only = resolve_inputs(s, s.get(Activity, ids["hr_only"]))  # type: ignore[arg-type]
        assert hr_only.ftp == 320.0 and hr_only.ftp_source == "activity.icu_ftp"
        assert hr_only.lthr == 160  # latest settings row still supplies HR anchors
        assert hr_only.has_power is False


def test_analyze_pending_writes_metrics_and_clears_flags(
    factory: sessionmaker[Session], store: StreamStore
) -> None:
    ids = _seed(factory, store)
    summary = analyze_pending(factory, store=store)
    counts = summary.counts()
    assert counts == {"analyzed": 2, "skipped_not_ride": 1, "skipped_no_streams": 1}
    with factory() as s:
        rows = {r.activity_id: r for r in ActivityMetricsRepo(s).all_rows()}
        assert set(rows) == {ids["power"], ids["hr_only"]}
        p = rows[ids["power"]]
        assert p.algo_version == ALGO_VERSION and p.tss_source == "power"
        assert p.np_w == pytest.approx(200.0) and p.tss == pytest.approx(64.0)
        assert p.comparison["tss_delta"] == pytest.approx(2.0)
        assert p.comparison["np_delta"] == pytest.approx(1.0)
        assert p.explanation["key"] == f"ride:{ids['power']}"
        assert p.status == "NORMAL" and p.next_recommendation == "AS_PLANNED"
        h = rows[ids["hr_only"]]
        assert h.tss_source == "hr" and h.tss == pytest.approx(100.0, abs=0.5)
        assert h.comparison["ftp_used"] == 320.0
        acts = {a.id: a for a in s.query(Activity).all()}
        assert acts[ids["power"]].pending_analysis is False
        assert acts[ids["power"]].stage_flags["analyzed"] is True
        assert acts[ids["strength"]].pending_analysis is False  # marked done without metrics
        assert acts[ids["no_streams"]].pending_analysis is True  # left in the queue
        run = JobRunRepo(s).latest("analyze")
        assert run is not None and run.status == "ok"
        assert run.counts["analyzed"] == 2

    # Second pass: nothing pending, versions match -> up_to_date only for explicit ids.
    again = analyze_pending(factory, store=store)
    assert again.counts() == {"skipped_no_streams": 1}  # only the stream-less ride stays queued
    explicit = analyze_pending(factory, store=store, activity_ids=[ids["power"]])
    assert explicit.counts() == {"up_to_date": 1}
    forced = analyze_pending(factory, store=store, force=True)
    assert forced.counts() == {"analyzed": 2}


def test_version_bump_triggers_recompute(
    factory: sessionmaker[Session], store: StreamStore
) -> None:
    ids = _seed(factory, store)
    analyze_pending(factory, store=store)
    with factory() as s:
        row = s.get(ActivityMetrics, ids["power"])
        assert row is not None
        row.algo_version = "ride-0.0.1"
        s.commit()
    summary = analyze_pending(factory, store=store)
    assert summary.counts()["analyzed"] == 1
    assert [r.activity_id for r in summary.results if r.outcome == "analyzed"] == [ids["power"]]
    with factory() as s:
        assert s.get(ActivityMetrics, ids["power"]).algo_version == ALGO_VERSION  # type: ignore[union-attr]


def test_analyze_activity_errors_and_limit(
    factory: sessionmaker[Session], store: StreamStore
) -> None:
    ids = _seed(factory, store)
    with factory() as s, pytest.raises(NotFoundError):
        analyze_activity(s, 9999, store=store)
    limited = analyze_pending(factory, store=store, limit=1)
    assert len(limited.results) == 1
    # A failing id inside the batch is recorded, not raised.
    summary = analyze_pending(factory, store=store, activity_ids=[9999, ids["power"]])
    assert summary.counts() == {"failed": 1, "analyzed": 1}
    assert summary.results[0].error and "NotFoundError" in summary.results[0].error


def test_cli_analyze_and_explain(data_dir: Path, db_url: str, store: StreamStore) -> None:
    from cyp.store.db import make_engine, session_factory
    from cyp.store.migrate import upgrade_head

    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url, "INTERVALS_API_KEY": "x"}
    runner = CliRunner()
    upgrade_head(db_url)
    engine = make_engine(db_url)
    ids = _seed(session_factory(engine), store)
    engine.dispose()

    result = runner.invoke(app, ["analyze"], env=env)
    assert result.exit_code == 0, result.output
    assert f"analyze (algo {ALGO_VERSION}" in result.output
    assert "analyzed: 2" in result.output
    assert f"ride:{ids['power']}  TSS 64 (power)  NP 200" in result.output

    explained = runner.invoke(app, ["explain", f"ride:{ids['power']}"], env=env)
    assert explained.exit_code == 0, explained.output
    assert "activity_metrics" in explained.output and "headline_zh" in explained.output

    failed = runner.invoke(app, ["analyze", "--activity", "4242"], env=env)
    assert failed.exit_code == 1
    assert "FAILED" in failed.output
