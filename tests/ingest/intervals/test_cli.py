"""`cyp sync intervals` and the intervals.icu section of `cyp doctor` (respx, no network)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cyp.cli import app
from cyp.store.db import make_engine, session_factory
from cyp.store.repo import ActivityRepo, IcuEventRepo, JobRunRepo, WellnessRepo
from tests.ingest.intervals.conftest import ATHLETE_ID, FAKE_KEY, FakeIcu

runner = CliRunner()


def _env(data_dir: Path, db_url: str, **extra: str) -> dict[str, str]:
    return {
        "CYP_DATA_DIR": str(data_dir),
        "CYP_DB_URL": db_url,
        "INTERVALS_API_KEY": FAKE_KEY,
        "STRAVA_ENABLED": "false",
        **extra,
    }


def _bootstrap(env: dict[str, str]) -> None:
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    assert runner.invoke(app, ["db", "upgrade"], env=env).exit_code == 0


def test_bare_sync_requires_migrated_schema(data_dir: Path, db_url: str) -> None:
    result = runner.invoke(app, ["sync"], env=_env(data_dir, db_url))
    assert result.exit_code == 2, result.output
    assert "run `cyp init` and `cyp db upgrade`" in result.output


def test_unified_sync_and_backfill_end_to_end(
    icu: FakeIcu, data_dir: Path, db_url: str, athlete_yaml: Path
) -> None:
    env = _env(data_dir, db_url)  # STRAVA_ENABLED=false -> intervals -> match only
    _bootstrap(env)
    result = runner.invoke(app, ["sync", "--no-streams"], env=env)
    assert result.exit_code == 0, result.output
    assert "intervals.icu" in result.output and "activities_new=3" in result.output
    assert "streams_written" not in result.output and icu.r_streams.call_count == 0
    assert "match (after intervals): " in result.output
    assert "rows_single_source=2" in result.output and "rows_strava_id=1" in result.output
    assert "skipped (STRAVA_ENABLED=false)" in result.output
    assert "match (after strava)" not in result.output

    engine = make_engine(db_url)
    with session_factory(engine)() as s:
        assert ActivityRepo(s).count() == 3
        strava_origin = ActivityRepo(s).get_by_intervals_id("i3002")
        assert strava_origin is not None and strava_origin.match_method == "strava_id"
        latest = JobRunRepo(s).latest_per_job()
        assert latest["sync"].status == "ok" and latest["match"].status == "ok"
        assert latest["sync:icu:activities"].status == "ok"
        assert "sync:strava" not in latest
    engine.dispose()

    doctor = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert doctor.exit_code == 0, doctor.output
    assert "activities: 3 rows (single_source=2, strava_id=1)" in doctor.output
    assert "WARN rides without streams: 2 of 2" in doctor.output  # --no-streams above
    assert "OK   last unified sync: sync ok" in doctor.output

    back = runner.invoke(app, ["backfill", "--days", "40"], env=env)
    assert back.exit_code == 0, back.output
    assert "backfill: " in back.output and "pages=2" in back.output
    assert "streams_written=1" in back.output
    engine = make_engine(db_url)
    with session_factory(engine)() as s:
        latest = JobRunRepo(s).latest_per_job()
        assert latest["backfill"].status == "ok" and latest["backfill:icu"].status == "ok"
    engine.dispose()
    doctor = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert "OK   last unified sync: backfill ok" in doctor.output
    assert "WARN rides without streams: 1 of 2" in doctor.output  # i3002: Strava-origin


def test_sync_intervals_requires_key(data_dir: Path, db_url: str) -> None:
    env = _env(data_dir, db_url)
    env["INTERVALS_API_KEY"] = ""
    result = runner.invoke(app, ["sync", "intervals"], env=env)
    assert result.exit_code == 2
    assert "INTERVALS_API_KEY" in result.output


def test_sync_intervals_rejects_unknown_stage(data_dir: Path, db_url: str) -> None:
    result = runner.invoke(
        app, ["sync", "intervals", "--stage", "bogus"], env=_env(data_dir, db_url)
    )
    assert result.exit_code == 2
    assert "unknown stage" in result.output


def test_sync_intervals_end_to_end_and_doctor(
    icu: FakeIcu, data_dir: Path, db_url: str, athlete_yaml: Path
) -> None:
    env = _env(data_dir, db_url)
    _bootstrap(env)

    before = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert "intervals.icu" in before.output
    assert "api key: present" in before.output
    assert "athlete id: unresolved" in before.output
    assert FAKE_KEY not in before.output

    result = runner.invoke(app, ["sync", "intervals", "--no-streams"], env=env)
    assert result.exit_code == 0, result.output
    assert "match: " in result.output and "rows_strava_id=1" in result.output
    assert "athlete: athlete=1, settings_history_added=1" in result.output
    assert "activities:" in result.output and "activities_new=3" in result.output
    assert "streams_written" not in result.output
    assert "wellness: wellness_days=10" in result.output
    assert "power_curves: power_curves=3" in result.output
    assert "events:" in result.output
    assert "rate limit: 2487/2500 remaining" in result.output
    assert icu.r_streams.call_count == 0
    assert icu.r_athlete0.call_count == 1  # athlete id resolved from /athlete/0

    engine = make_engine(db_url)
    with session_factory(engine)() as s:
        assert ActivityRepo(s).count() == 3
        assert IcuEventRepo(s).count() == 6
        assert WellnessRepo(s).latest_date(1) is not None
    engine.dispose()

    after = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert after.exit_code == 0, after.output
    assert f"OK   athlete id: {ATHLETE_ID}" in after.output
    assert "cursor activities_newest: 2" in after.output  # today's date (local)
    assert "cursor wellness_newest: 2" in after.output
    assert "last runs" not in after.output  # nothing failed

    # Streams can be fetched later for the pending ride.
    again = runner.invoke(app, ["sync", "intervals", "--stage", "activities"], env=env)
    assert again.exit_code == 0, again.output
    assert "streams_written=1" in again.output
    assert (data_dir / "streams").glob("*.parquet")


def test_sync_intervals_backfill_option(icu: FakeIcu, data_dir: Path, db_url: str) -> None:
    env = _env(data_dir, db_url)
    _bootstrap(env)
    result = runner.invoke(app, ["sync", "intervals", "--backfill-days", "40"], env=env)
    assert result.exit_code == 0, result.output
    assert "backfill: " in result.output and "pages=2" in result.output
    assert icu.r_activities.call_count == 2


def test_doctor_without_key_fails(data_dir: Path, db_url: str, athlete_yaml: Path) -> None:
    env = _env(data_dir, db_url)
    env["INTERVALS_API_KEY"] = ""
    _bootstrap(env)
    result = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert result.exit_code == 1
    assert "FAIL api key: INTERVALS_API_KEY missing" in result.output
