"""Shared intervals.icu test helpers: fixture payloads, a respx router, a no-sleep client."""

from __future__ import annotations

import copy
import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from sqlalchemy.orm import Session, sessionmaker

from cyp.ingest.intervals.auth import ApiKeyAuth
from cyp.ingest.intervals.client import BASE_URL, IntervalsClient
from cyp.ingest.intervals.sync import IntervalsSyncer, SyncOptions
from cyp.store.streams import StreamStore

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "intervals"
ATHLETE_ID = "i123456"
FAKE_KEY = "fixture-api-key-not-real"
#: Fixed "now" for every sync test: 2026-10-02 08:00 Asia/Taipei.
NOW_LOCAL = dt.datetime(2026, 10, 2, 8, 0, tzinfo=ZoneInfo("Asia/Taipei"))
TODAY = NOW_LOCAL.date()

RATE_HEADERS = {"X-RateLimit-Limit": "2500", "X-RateLimit-Remaining": "2487"}


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(payload: Any, **headers: str) -> httpx.Response:
    return httpx.Response(200, json=payload, headers={**RATE_HEADERS, **headers})


class Sleeper:
    """Records requested sleeps instead of sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeIcu:
    """respx routes for every endpoint the syncer touches, backed by mutable fixture copies."""

    def __init__(self, router: respx.MockRouter) -> None:
        self.router = router
        self.athlete = load_fixture("athlete.json")
        self.sport_settings = load_fixture("sport_settings.json")
        self.activities = load_fixture("activities.json")
        self.streams = {"i3001": load_fixture("streams_i3001.json"), "i3003": []}
        self.intervals = {"i3001": load_fixture("intervals_i3001.json")}
        self.power_curves = load_fixture("power_curves.json")
        self.mmp_model = load_fixture("mmp_model.json")
        self.wellness = load_fixture("wellness.json")
        self.events = load_fixture("events.json")
        self.fitness_model_events = load_fixture("fitness_model_events.json")

        a = f"/athlete/{ATHLETE_ID}"
        self.r_athlete0 = router.get("/athlete/0").mock(side_effect=lambda _r: ok(self.athlete))
        self.r_athlete = router.get(a).mock(side_effect=lambda _r: ok(self.athlete))
        self.r_settings = router.get(f"{a}/sport-settings").mock(
            side_effect=lambda _r: ok(self.sport_settings)
        )
        self.r_activities = router.get(f"{a}/activities").mock(side_effect=self._list_activities)
        self.r_streams = router.get(url__regex=r".*/activity/(?P<aid>[^/]+)/streams\.json").mock(
            side_effect=lambda _r, aid: ok(self.streams.get(aid, []))
        )
        self.r_intervals = router.get(url__regex=r".*/activity/(?P<aid>[^/]+)/intervals$").mock(
            side_effect=lambda _r, aid: ok(self.intervals.get(aid, {"icu_intervals": []}))
        )
        self.r_power_curves = router.get(f"{a}/power-curves.json").mock(
            side_effect=lambda _r: ok(self.power_curves)
        )
        self.r_mmp = router.get(f"{a}/mmp-model").mock(side_effect=lambda _r: ok(self.mmp_model))
        self.r_wellness = router.get(f"{a}/wellness.json").mock(side_effect=self._list_wellness)
        self.r_events = router.get(f"{a}/events.json").mock(side_effect=lambda _r: ok(self.events))
        self.r_fitness_events = router.get(f"{a}/fitness-model-events").mock(
            side_effect=lambda _r: ok(self.fitness_model_events)
        )
        self.r_activity_curve = router.get(
            url__regex=r".*/activity/(?P<aid>[^/]+)/power-curve\.json"
        ).mock(return_value=httpx.Response(200, headers=RATE_HEADERS))

    # Windowed endpoints filter the fixture by the requested local dates, like icu does.
    def _list_activities(self, request: httpx.Request) -> httpx.Response:
        oldest = request.url.params.get("oldest", "0000")[:10]
        newest = request.url.params.get("newest", "9999")[:10]
        rows = [a for a in self.activities if oldest <= str(a["start_date_local"])[:10] <= newest]
        return ok(rows)

    def _list_wellness(self, request: httpx.Request) -> httpx.Response:
        oldest = request.url.params.get("oldest", "0000")[:10]
        newest = request.url.params.get("newest", "9999")[:10]
        return ok([w for w in self.wellness if oldest <= w["id"] <= newest])

    def activity_requests(self) -> list[tuple[str, str]]:
        """``(oldest, newest)`` query pairs of every activities list call, in order."""
        return [
            (c.request.url.params.get("oldest", ""), c.request.url.params.get("newest", ""))
            for c in self.r_activities.calls
        ]

    def ride(self) -> dict[str, Any]:
        """The Garmin-origin Ride fixture (mutable)."""
        return next(a for a in self.activities if a["id"] == "i3001")

    def clone_ride(self, new_id: str, start_local: str) -> dict[str, Any]:
        """Add another Garmin ride (same streams) on ``start_local``; returns it."""
        extra = copy.deepcopy(self.ride())
        extra["id"] = new_id
        extra["start_date_local"] = start_local
        extra["start_date"] = None
        self.activities.append(extra)
        self.streams[new_id] = self.streams["i3001"]
        self.intervals[new_id] = self.intervals["i3001"]
        return extra


@pytest.fixture
def icu() -> Iterator[FakeIcu]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield FakeIcu(router)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def client(sleeper: Sleeper) -> Iterator[IntervalsClient]:
    c = IntervalsClient(
        auth=ApiKeyAuth(FAKE_KEY), athlete_id="0", sleep=sleeper, backoff_base_s=0.25
    )
    yield c
    c.close()


@pytest.fixture
def stream_store(data_dir: Path) -> StreamStore:
    return StreamStore(data_dir / "streams")


@pytest.fixture
def syncer(
    client: IntervalsClient, factory: sessionmaker[Session], stream_store: StreamStore
) -> IntervalsSyncer:
    return IntervalsSyncer(
        client,
        factory,
        stream_store,
        options=SyncOptions(initial_days=30),
        timezone="Asia/Taipei",
        clock=lambda: NOW_LOCAL,
    )
