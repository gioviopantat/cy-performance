"""Shared fixtures: isolated data dir + SQLite DB per test, env scrubbed of real .env values."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cyp.settings import Settings, get_settings
from cyp.store.db import make_engine, session_factory
from cyp.store.migrate import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[1]
ATHLETE_YAML = REPO_ROOT / "config" / "athlete.yaml"

_ENV_KEYS = [name.upper() for name in Settings.model_fields] + [
    "STRAVA_ENABLED",
    "CYP_DATA_DIR",
    "CYP_DB_URL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Remove every Settings env var and point pydantic-settings at a non-existent .env."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # so a developer's real .env is never picked up


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def db_url(data_dir: Path) -> str:
    return f"sqlite:///{data_dir / 'cyp.sqlite'}"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, data_dir: Path, db_url: str) -> Settings:
    monkeypatch.setenv("CYP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CYP_DB_URL", db_url)
    return get_settings()


@pytest.fixture
def migrated_engine(db_url: str) -> Iterator[Engine]:
    upgrade_head(db_url)
    engine = make_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return session_factory(migrated_engine)


@pytest.fixture
def athlete_yaml(tmp_path: Path) -> Path:
    """Copy of the repo's athlete.yaml inside tmp (tests may mutate it)."""
    target = tmp_path / "athlete.yaml"
    shutil.copy(ATHLETE_YAML, target)
    return target
