"""New CLI commands: ftp (+ accept), season, --json outputs, dev seed / bench / openapi, serve."""

from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from cyp.cli import app
from cyp.devtools.synthetic import seed_synthetic
from cyp.store.db import make_engine, session_factory
from cyp.store.migrate import upgrade_head
from cyp.store.models import Activity, AthleteSettingsHistory
from cyp.store.streams import StreamStore

runner = CliRunner()
END = dt.date(2026, 10, 4)  # last seeded day; the season starts 2026-10-05


@pytest.fixture(scope="module")
def seeded_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 30-day synthetic store built once per module (tests copy it)."""
    data = tmp_path_factory.mktemp("seeded") / "data"
    url = f"sqlite:///{data / 'cyp.sqlite'}"
    data.mkdir(parents=True)
    upgrade_head(url)
    engine = make_engine(url)
    try:
        seed_synthetic(session_factory(engine), StreamStore(data / "streams"), days=30, end=END)
    finally:
        engine.dispose()
    return data


@pytest.fixture
def seeded(seeded_template: Path, data_dir: Path, db_url: str) -> dict[str, str]:
    shutil.copytree(seeded_template, data_dir)
    return {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}


def _json(stdout: str) -> object:
    return json.loads(stdout)


def test_ftp_text_json_and_what_if(seeded: dict[str, str]) -> None:
    res = runner.invoke(app, ["ftp", "--date", END.isoformat()], env=seeded)
    assert res.exit_code == 0, res.output
    assert f"FTP as of {END}:" in res.output and "W/kg" in res.output
    assert "42d cp_2p: CP" in res.output and "90d" in res.output
    assert "estimate source:" in res.output
    assert "FTP proposal:" in res.output and "compute_ms:" in res.output

    res = runner.invoke(app, ["ftp", "--date", END.isoformat(), "--json"], env=seeded)
    assert res.exit_code == 0, res.output
    blob = _json(res.stdout)
    assert isinstance(blob, dict)
    assert blob["as_of"] == END.isoformat() and blob["current_ftp"] > 200
    assert set(blob["windows"]) >= {"42d", "90d"}

    res = runner.invoke(
        app, ["ftp", "--date", END.isoformat(), "--what-if", "230", "--json"], env=seeded
    )
    assert res.exit_code == 0, res.output
    assert _json(res.stdout)["current_ftp"] == 230  # type: ignore[index]
    text = runner.invoke(app, ["ftp", "--what-if", "230"], env=seeded)
    assert text.exit_code == 0 and "(what-if FTP 230 W)" in text.output

    bad = runner.invoke(app, ["ftp", "--date", "04-10-2026"], env=seeded)
    assert bad.exit_code == 2


def test_ftp_accept_requires_yes(seeded: dict[str, str], db_url: str) -> None:
    start = END - dt.timedelta(days=6)
    dry = runner.invoke(app, ["ftp", "accept", "270", "--from", start.isoformat()], env=seeded)
    assert dry.exit_code == 2, dry.output
    assert "would record FTP 270 W" in dry.output and "--yes" in dry.output
    engine = make_engine(db_url)
    factory = session_factory(engine)
    with factory() as s:
        assert not s.scalars(
            select(AthleteSettingsHistory).where(AthleteSettingsHistory.source == "manual")
        ).all()
        s.execute(Activity.__table__.update().values(pending_analysis=False))
        s.commit()

    res = runner.invoke(
        app, ["ftp", "accept", "270", "--from", start.isoformat(), "--yes"], env=seeded
    )
    assert res.exit_code == 0, res.output
    assert "FTP 270 W recorded" in res.output
    assert "intervals.icu" in res.output and "cyp analyze" in res.output
    with factory() as s:
        manual = s.scalars(
            select(AthleteSettingsHistory).where(AthleteSettingsHistory.source == "manual")
        ).one()
        assert manual.ftp == 270.0 and manual.effective_from == start
        rides = s.scalars(select(Activity).where(Activity.is_ride.is_(True))).all()
        after = [a for a in rides if a.start_utc >= start.isoformat()]
        before = [a for a in rides if a.start_utc < start.isoformat()]
        assert after and all(a.pending_analysis for a in after)
        assert before and not any(a.pending_analysis for a in before)
    engine.dispose()


def test_season_table_and_json(seeded: dict[str, str], athlete_yaml: Path) -> None:
    cfg = ["--athlete-config", str(athlete_yaml)]
    res = runner.invoke(app, ["season", *cfg], env=seeded)
    assert res.exit_code == 0, res.output
    lines = [ln for ln in res.output.splitlines() if ln.strip()[:1].isdigit()]
    assert len(lines) == 26
    assert "season 2026-10-05 ->" in res.output and "CTL start" in res.output
    res = runner.invoke(app, ["season", "--json", *cfg], env=seeded)
    assert res.exit_code == 0, res.output
    blob = _json(res.stdout)
    assert isinstance(blob, dict) and len(blob["weeks"]) == 26
    assert blob["weeks"][0]["index"] == 1
    missing = runner.invoke(app, ["season"], env=seeded)
    assert missing.exit_code == 2


def test_plan_dry_run_json(seeded: dict[str, str], athlete_yaml: Path) -> None:
    res = runner.invoke(
        app,
        [
            "plan",
            "--date",
            "2026-10-06",
            "--dry-run",
            "--json",
            "--athlete-config",
            str(athlete_yaml),
        ],
        env=seeded,
    )
    assert res.exit_code in (0, 3), res.output
    blob = _json(res.stdout)
    assert isinstance(blob, dict)
    assert blob["persisted"] is False and blob["today"] == "2026-10-06"
    assert blob["days"] and blob["weeks"]
    assert blob["season_key"] == "s20261005"


def test_trends_and_readiness_json(seeded: dict[str, str]) -> None:
    res = runner.invoke(app, ["trends", "--date", END.isoformat(), "--json"], env=seeded)
    assert res.exit_code == 0, res.output
    blob = _json(res.stdout)
    assert isinstance(blob, dict)
    assert blob["as_of"] == END.isoformat() and "cp_fits" in blob and "limiters" in blob
    text = runner.invoke(app, ["trends", "--date", END.isoformat()], env=seeded)
    assert f"trends as of {END}" in text.output and "PMC vs icu" in text.output

    res = runner.invoke(
        app, ["readiness", "--date", END.isoformat(), "--days", "2", "--json"], env=seeded
    )
    assert res.exit_code == 0, res.output
    verdicts = _json(res.stdout)
    assert isinstance(verdicts, list) and len(verdicts) == 2
    assert verdicts[-1]["date"] == END.isoformat()


def test_dev_seed_refuses_non_empty_and_bench(
    data_dir: Path, db_url: str, athlete_yaml: Path
) -> None:
    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    res = runner.invoke(app, ["dev", "seed", "--days", "30"], env=env)
    assert res.exit_code == 0, res.output
    assert "seeded 30 days" in res.output and "rides" in res.output
    again = runner.invoke(app, ["dev", "seed", "--days", "30"], env=env)
    assert again.exit_code == 2 and "already has an athlete" in again.output

    bench = runner.invoke(
        app, ["dev", "bench", "--repeat", "1", "--athlete-config", str(athlete_yaml)], env=env
    )
    assert bench.exit_code == 0, bench.output
    assert "median_ms" in bench.output and "trends" in bench.output
    as_json = runner.invoke(
        app,
        ["dev", "bench", "--repeat", "1", "--json", "--athlete-config", str(athlete_yaml)],
        env=env,
    )
    assert as_json.exit_code == 0, as_json.output
    blob = _json(as_json.stdout)
    assert isinstance(blob, dict) and {"trends", "plan_14d"} <= set(blob)


def test_dev_bench_on_empty_store_fails_cleanly(
    data_dir: Path, db_url: str, athlete_yaml: Path
) -> None:
    upgrade_head(db_url)
    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    res = runner.invoke(
        app, ["dev", "bench", "--repeat", "1", "--athlete-config", str(athlete_yaml)], env=env
    )
    assert res.exit_code == 1 and "bench failed" in res.output


def test_openapi_and_serve_help_and_missing_api(
    data_dir: Path, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    for args in (["dev", "openapi", "--help"], ["serve", "--help"]):
        res = runner.invoke(app, args, env=env)
        assert res.exit_code == 0, res.output
    # Simulate an install without the API module (None in sys.modules -> ImportError).
    monkeypatch.setitem(sys.modules, "cyp.api.app", None)
    res = runner.invoke(app, ["dev", "openapi", "--out", str(data_dir / "o.json")], env=env)
    assert res.exit_code == 1 and "HTTP API is not available" in res.output
    res = runner.invoke(app, ["serve", "--host", "0.0.0.0"], env=env)
    assert res.exit_code == 1 and "HTTP API is not available" in res.output


def test_serve_loopback_detection() -> None:
    from cyp.cli.serve import is_loopback

    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("192.168.1.10")


def test_outdated_schema_gives_upgrade_hint(
    data_dir: Path, db_url: str, athlete_yaml: Path
) -> None:
    """Regression: a DB one migration behind used to crash with a raw OperationalError."""
    from alembic import command

    from cyp.store.migrate import alembic_config, upgrade_head

    upgrade_head(db_url)
    command.downgrade(alembic_config(db_url), "-1")
    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    res = CliRunner().invoke(app, ["dev", "bench", "--athlete-config", str(athlete_yaml)], env=env)
    assert res.exit_code == 2
    assert "cyp db upgrade" in res.output and "Traceback" not in res.output
