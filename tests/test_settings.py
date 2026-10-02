"""Settings parsing from env / .env and secret masking."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cyp.settings import Settings, get_settings


def test_defaults() -> None:
    s = get_settings()
    assert s.strava_enabled is True
    assert s.cyp_data_dir == Path("data")
    assert s.cyp_db_url == "sqlite:///data/cyp.sqlite"
    assert s.cyp_plan_mode == "propose"
    assert s.cyp_timezone == "Asia/Taipei"
    assert s.cyp_plan_horizon_days == 14


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STRAVA_ENABLED", "false")
    monkeypatch.setenv("CYP_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("CYP_DB_URL", "sqlite:///x.sqlite")
    monkeypatch.setenv("CYP_PLAN_MODE", "apply")
    monkeypatch.setenv("CYP_TIMEZONE", "UTC")
    monkeypatch.setenv("INTERVALS_API_KEY", "secret-key")
    s = get_settings()
    assert s.strava_enabled is False
    assert s.cyp_data_dir == tmp_path / "d"
    assert s.cyp_db_url == "sqlite:///x.sqlite"
    assert s.cyp_plan_mode == "apply"
    assert s.cyp_timezone == "UTC"
    assert s.intervals_api_key.get_secret_value() == "secret-key"
    assert s.streams_dir == tmp_path / "d" / "streams"
    assert s.logs_dir == tmp_path / "d" / "logs"


def test_dotenv_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "STRAVA_CLIENT_ID=123\nSTRAVA_CLIENT_SECRET=shh\nCYP_PLAN_MODE=apply\n", encoding="utf-8"
    )
    s = Settings()  # cwd is tmp_path via autouse fixture
    assert s.strava_client_id == "123"
    assert s.strava_client_secret.get_secret_value() == "shh"
    assert s.cyp_plan_mode == "apply"


def test_invalid_plan_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CYP_PLAN_MODE", "yolo")
    with pytest.raises(ValidationError):
        get_settings()


def test_masked_summary_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERVALS_API_KEY", "secret-key")
    summary = get_settings().masked_summary()
    assert summary["intervals_api_key"] == "***"
    assert summary["anthropic_api_key"] == "(unset)"
    assert "secret-key" not in repr(summary)
    assert "secret-key" not in repr(get_settings())
