"""CLI smoke tests via typer's CliRunner."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from typer.testing import CliRunner

from cyp.cli import app
from cyp.store.db import make_engine, session_factory
from cyp.store.models import Athlete, ReadinessDaily

runner = CliRunner()


def _env(data_dir: Path, db_url: str, **extra: str) -> dict[str, str]:
    return {
        "CYP_DATA_DIR": str(data_dir),
        "CYP_DB_URL": db_url,
        "INTERVALS_API_KEY": SECRET,
        **extra,
    }


SECRET = "s3cr3t-api-key-value"


def test_init_creates_layout(data_dir: Path, db_url: str) -> None:
    result = runner.invoke(app, ["init"], env=_env(data_dir, db_url))
    assert result.exit_code == 0, result.output
    for sub in ("streams", "tokens", "logs", "reports"):
        assert (data_dir / sub).is_dir()
    assert (data_dir / "logs" / "cyp.jsonl").is_file()


def test_doctor_before_init_reports_problems(data_dir: Path, db_url: str) -> None:
    result = runner.invoke(app, ["doctor"], env=_env(data_dir, db_url))
    assert result.exit_code == 1
    assert "missing (run `cyp init`)" in result.output
    assert "(run `cyp db upgrade`)" in result.output
    assert "intervals_api_key: ***" in result.output
    assert SECRET not in result.output


def test_doctor_after_init_and_upgrade(data_dir: Path, db_url: str, athlete_yaml: Path) -> None:
    env = _env(data_dir, db_url, STRAVA_ENABLED="false")
    assert runner.invoke(app, ["init"], env=env).exit_code == 0
    up = runner.invoke(app, ["db", "upgrade"], env=env)
    assert up.exit_code == 0, up.output
    assert "schema at" in up.output
    result = runner.invoke(app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert result.exit_code == 0, result.output
    assert "all checks passed" in result.output
    assert "WARN " not in result.output  # no legacy token-file rows any more
    assert "strava: disabled (STRAVA_ENABLED=false)" in result.output
    assert "valid; season ftp_target" in result.output
    assert "Matching" in result.output and "activities: 0 rows (none)" in result.output
    assert "OK   rides without streams: 0 of 0" in result.output
    assert "last unified sync: never (run `cyp sync`" in result.output
    assert "daily: never run" in result.output and "sync:strava: never run" in result.output
    assert "current=" in result.output and "head=" in result.output


def test_doctor_flags_bad_athlete_config(data_dir: Path, db_url: str, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("timezone: UTC\n")
    result = runner.invoke(
        app, ["doctor", "--athlete-config", str(bad)], env=_env(data_dir, db_url)
    )
    assert result.exit_code == 1
    assert "FAIL " in result.output and "invalid athlete config" in result.output


def test_explain_not_found_and_found(data_dir: Path, db_url: str) -> None:
    env = _env(data_dir, db_url)
    assert runner.invoke(app, ["db", "upgrade"], env=env).exit_code == 0
    missing = runner.invoke(app, ["explain", "readiness.verdict"], env=env)
    assert missing.exit_code == 1
    assert "no explanation found" in missing.output

    engine = make_engine(db_url)
    with session_factory(engine)() as s:
        s.add(Athlete(id=1, name="test"))
        s.flush()
        s.add(
            ReadinessDaily(
                athlete_id=1,
                date_local=dt.date(2026, 10, 2),
                explanation={
                    "key": "readiness.verdict",
                    "headline_zh": "普通",
                    "because": [],
                    "method": None,
                    "confidence": "medium",
                    "glossary_terms": [],
                },
            )
        )
        s.commit()
    engine.dispose()
    found = runner.invoke(app, ["explain", "readiness.verdict"], env=env)
    assert found.exit_code == 0, found.output
    assert "(from readiness_daily)" in found.output
    assert "普通" in found.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("cyp ")
