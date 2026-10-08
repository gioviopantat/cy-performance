"""Shared fixtures: isolated data dir + SQLite DB per test, env scrubbed of real .env values."""

from __future__ import annotations

import shutil
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cyp.settings import Settings, get_settings
from cyp.store.db import make_engine, session_factory
from cyp.store.migrate import upgrade_head

REPO_ROOT = Path(__file__).resolve().parents[1]
ATHLETE_YAML = REPO_ROOT / "tests" / "fixtures" / "athlete.reference.yaml"

NO_NOTIFY = "notify.macos=off"


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _guarded(real: Any) -> Any:
    def connect(self: socket.socket, address: Any, *args: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else None
        if host is None or host in _LOOPBACK:  # Unix sockets and local test servers are fine
            return real(self, address, *args)
        raise RuntimeError(f"tests must not open network connections (use respx): {address}")

    return connect


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
    # No real desktop notifications from tests, also not from `run --all` child processes
    # (CYP_FEATURES is inherited by profile runs). Tests that set CYP_FEATURES keep this entry.
    monkeypatch.setenv("CYP_FEATURES", NO_NOTIFY)
    monkeypatch.setattr("cyp.jobs.notify.notify_macos", lambda *_a, **_k: False)
    monkeypatch.setattr("cyp.jobs.runner.notify_macos", lambda *_a, **_k: False)
    # No network: respx mocks httpx above the socket layer, so a real connect is always a bug.
    monkeypatch.setattr(socket.socket, "connect", _guarded(_REAL_CONNECT))
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded(_REAL_CONNECT_EX))
    # DNS is network too: the scheduled run's wait-for-network probe must not resolve real hosts.
    monkeypatch.setattr("cyp.jobs.runner.wait_for_network", lambda *_a, **_k: True)


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
    """Copy of the invented reference athlete inside tmp (tests may mutate it)."""
    target = tmp_path / "athlete.yaml"
    shutil.copy(ATHLETE_YAML, target)
    return target
