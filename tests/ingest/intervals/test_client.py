"""IntervalsClient: auth header, UA, athlete-id resolution, retries, 429, rate-limit headers."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import httpx
import pytest
import respx

from cyp.core.errors import IngestError, RateLimitError
from cyp.ingest.intervals.auth import ApiKeyAuth, AuthStrategy, BearerAuth
from cyp.ingest.intervals.client import (
    BASE_URL,
    DEFAULT_STREAM_TYPES,
    USER_AGENT,
    IntervalsClient,
)
from tests.ingest.intervals.conftest import ATHLETE_ID, FAKE_KEY, FakeIcu, Sleeper, ok


def test_auth_strategy_protocol_and_headers() -> None:
    key = ApiKeyAuth(FAKE_KEY)
    assert isinstance(key, AuthStrategy)
    assert isinstance(BearerAuth("tok"), AuthStrategy)
    expected = base64.b64encode(f"API_KEY:{FAKE_KEY}".encode()).decode()
    assert key.headers() == {"Authorization": f"Basic {expected}"}
    assert FAKE_KEY not in key.describe()
    with pytest.raises(ValueError, match="empty"):
        ApiKeyAuth("")


def test_requests_carry_basic_auth_and_browser_ua(icu: FakeIcu, client: IntervalsClient) -> None:
    athlete = client.get_athlete()
    assert athlete["id"] == ATHLETE_ID
    request = icu.r_athlete0.calls.last.request
    expected = base64.b64encode(f"API_KEY:{FAKE_KEY}".encode()).decode()
    assert request.headers["Authorization"] == f"Basic {expected}"
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["User-Agent"].startswith("Mozilla/5.0")
    assert request.headers["Accept"] == "application/json"


def test_athlete_id_resolved_once_and_cached(icu: FakeIcu, client: IntervalsClient) -> None:
    assert client.resolved_athlete_id is None
    assert client.resolve_athlete_id() == ATHLETE_ID
    assert client.resolve_athlete_id() == ATHLETE_ID
    client.get_sport_settings()
    client.list_wellness("2026-09-01", "2026-09-10")
    assert icu.r_athlete0.call_count == 1  # GET /athlete/0 exactly once
    assert icu.r_settings.calls.last.request.url.path.endswith(
        f"/athlete/{ATHLETE_ID}/sport-settings"
    )
    assert client.resolved_athlete_id == ATHLETE_ID


def test_configured_athlete_id_skips_resolution(icu: FakeIcu, sleeper: Sleeper) -> None:
    client = IntervalsClient(auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper)
    client.get_sport_settings()
    assert icu.r_athlete0.call_count == 0
    assert icu.r_settings.call_count == 1
    client.close()


def test_rate_limit_headers_recorded(icu: FakeIcu, client: IntervalsClient) -> None:
    client.get_athlete()
    assert client.rate_limit.limit == 2500
    assert client.rate_limit.remaining == 2487
    assert client.rate_limit.observed_at is not None
    snap = client.rate_limit.snapshot()
    assert snap["intervals_remaining"] == 2487 and snap["intervals_429_hits"] == 0


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL) as r:
        yield r


def test_429_honours_retry_after_then_succeeds(router: respx.MockRouter, sleeper: Sleeper) -> None:
    route = router.get(f"/athlete/{ATHLETE_ID}/wellness.json").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7", "X-RateLimit-Remaining": "0"}),
            ok([{"id": "2026-10-01", "ctl": 40.0}]),
        ]
    )
    client = IntervalsClient(auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper)
    rows = client.list_wellness("2026-10-01", "2026-10-01")
    assert rows == [{"id": "2026-10-01", "ctl": 40.0}]
    assert route.call_count == 2
    assert sleeper.calls == [7.0]
    assert client.rate_limit.hits_429 == 1
    assert client.rate_limit.retry_after_s == 7.0


def test_429_exhausted_raises_rate_limit_error(router: respx.MockRouter, sleeper: Sleeper) -> None:
    router.get(f"/athlete/{ATHLETE_ID}/events.json").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"})
    )
    client = IntervalsClient(
        auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper, max_retries=2
    )
    with pytest.raises(RateLimitError) as exc_info:
        client.list_events("2026-10-01", "2026-10-07")
    assert exc_info.value.retry_after_s == 3.0
    assert sleeper.calls == [3.0, 3.0]


def test_5xx_retries_with_backoff_then_succeeds(router: respx.MockRouter, sleeper: Sleeper) -> None:
    route = router.get("/activity/i1/intervals").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            ok({"id": "i1", "icu_intervals": []}),
        ]
    )
    client = IntervalsClient(
        auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper, backoff_base_s=0.5
    )
    assert client.get_activity_intervals("i1") == {"id": "i1", "icu_intervals": []}
    assert route.call_count == 3
    assert sleeper.calls == [0.5, 1.0]


def test_5xx_exhausted_raises_ingest_error(router: respx.MockRouter, sleeper: Sleeper) -> None:
    router.get("/activity/i1/intervals").mock(return_value=httpx.Response(500, text="boom"))
    client = IntervalsClient(
        auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper, max_retries=3
    )
    with pytest.raises(IngestError, match="500"):
        client.get_activity_intervals("i1")
    assert len(sleeper.calls) == 3


def test_4xx_is_not_retried(router: respx.MockRouter, sleeper: Sleeper) -> None:
    route = router.get("/activity/i404").mock(return_value=httpx.Response(404, text="nope"))
    client = IntervalsClient(auth=ApiKeyAuth(FAKE_KEY), athlete_id=ATHLETE_ID, sleep=sleeper)
    with pytest.raises(IngestError, match="404"):
        client.get_activity("i404")
    assert route.call_count == 1
    assert sleeper.calls == []


def test_typed_methods_send_documented_params(icu: FakeIcu, client: IntervalsClient) -> None:
    client.list_activities("2026-09-01", "2026-09-30", fields=["id", "icu_training_load"], limit=50)
    p = icu.r_activities.calls.last.request.url.params
    assert p["oldest"] == "2026-09-01" and p["newest"] == "2026-09-30"
    assert p["fields"] == "id,icu_training_load" and p["limit"] == "50"

    client.get_activity_streams("i3001")
    p = icu.r_streams.calls.last.request.url.params
    assert p["types"] == ",".join(DEFAULT_STREAM_TYPES)
    assert icu.r_streams.calls.last.request.url.path.endswith("/activity/i3001/streams.json")

    client.get_power_curves(type="Ride", curves=["42d", "90d", "s0"], newest="2026-10-02")
    p = icu.r_power_curves.calls.last.request.url.params
    assert p["type"] == "Ride" and p["curves"] == "42d,90d,s0" and p["newest"] == "2026-10-02"

    client.get_mmp_model("Ride")
    assert icu.r_mmp.calls.last.request.url.params["type"] == "Ride"

    client.list_events("2026-10-01", "2026-10-31", category=["WORKOUT", "NOTE"])
    p = icu.r_events.calls.last.request.url.params
    assert p["oldest"] == "2026-10-01" and p["newest"] == "2026-10-31"
    assert p["category"] == "WORKOUT,NOTE"

    assert client.get_fitness_model_events()[0]["category"] == "SET_EFTP"

    detail = icu.router.get("/activity/i3001").mock(return_value=ok(icu.ride()))
    assert client.get_activity("i3001", intervals=True)["id"] == "i3001"
    assert detail.calls.last.request.url.params["intervals"] == "true"
    assert client.get_activity_power_curve("i3001") == {}  # empty body -> {}
