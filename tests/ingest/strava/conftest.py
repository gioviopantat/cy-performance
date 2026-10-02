"""Shared Strava test helpers: fixture payloads, a fake token store, a respx-backed client."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from cyp.ingest.strava.client import BASE_URL, StravaClient
from cyp.ingest.strava.oauth import StravaToken, TokenStore
from cyp.settings import Settings, get_settings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "strava"

# Made-up values; nothing here comes from a real token file.
FAKE_ACCESS = "fixture-access-token-A"
FAKE_REFRESH = "fixture-refresh-token-A"
FAKE_CLIENT_ID = "12345"
FAKE_CLIENT_SECRET = "fixture-client-secret"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def api(path: str) -> str:
    return f"{BASE_URL}{path}"


def ok(
    payload: Any, *, usage: str = "10,100", limit: str = "200,2000", **headers: str
) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        headers={"X-RateLimit-Usage": usage, "X-RateLimit-Limit": limit, **headers},
    )


@pytest.fixture
def strava_settings(monkeypatch: pytest.MonkeyPatch, data_dir: Path, db_url: str) -> Settings:
    monkeypatch.setenv("CYP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CYP_DB_URL", db_url)
    monkeypatch.setenv("STRAVA_CLIENT_ID", FAKE_CLIENT_ID)
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", FAKE_CLIENT_SECRET)
    return get_settings()


@pytest.fixture
def valid_token() -> StravaToken:
    return StravaToken(
        access_token=SecretStr(FAKE_ACCESS),
        refresh_token=SecretStr(FAKE_REFRESH),
        expires_at=int(time.time()) + 3600,
        scope="activity:read_all,profile:read_all",
        athlete_id=188844906,
    )


@pytest.fixture
def token_store(strava_settings: Settings, valid_token: StravaToken) -> TokenStore:
    store = TokenStore.from_settings(strava_settings)
    store.save(valid_token)
    return store


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeper() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def client(sleeper: SleepRecorder) -> StravaClient:
    """Client with a static token and recorded sleeps (respx intercepts the httpx.Client)."""
    return StravaClient(
        lambda: FAKE_ACCESS,
        lambda: "fixture-access-token-refreshed",
        http=httpx.Client(),
        sleep=sleeper,
    )
