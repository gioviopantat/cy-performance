"""StravaClient: rate-limit header parsing, wait decision, 429/401/5xx handling, pagination."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from cyp.core.errors import IngestError, RateLimitError
from cyp.ingest.strava import client as client_mod
from cyp.ingest.strava.client import (
    DEFAULT_STREAM_KEYS,
    RateLimitState,
    StravaClient,
    parse_pair,
    seconds_until_next_window,
)
from tests.ingest.strava.conftest import FAKE_ACCESS, SleepRecorder, api, load_fixture, ok


def _epoch(h: int, m: int, s: int = 0) -> float:
    return dt.datetime(2026, 10, 2, h, m, s, tzinfo=dt.UTC).timestamp()


def test_seconds_until_next_window() -> None:
    assert seconds_until_next_window(_epoch(10, 0)) == 0
    assert seconds_until_next_window(_epoch(10, 15)) == 0
    assert seconds_until_next_window(_epoch(10, 7, 30)) == 450
    assert seconds_until_next_window(_epoch(10, 59, 59)) == 1
    assert 0 <= seconds_until_next_window() < client_mod.WINDOW_S


def test_parse_pair() -> None:
    assert parse_pair("10, 100") == (10, 100)
    assert parse_pair("199,1500") == (199, 1500)
    assert parse_pair(None) is None
    assert parse_pair("x,y") is None
    assert parse_pair("1,2,3") is None


def test_rate_limit_state_remaining_and_wait() -> None:
    state = RateLimitState()
    assert state.remaining_15m() is None
    assert state.wait_needed_s() == 0
    now = _epoch(10, 5)
    state.record(
        httpx.Headers(
            {
                "X-RateLimit-Usage": "95,500",
                "X-RateLimit-Limit": "200,2000",
                "X-ReadRateLimit-Usage": "95,500",
                "X-ReadRateLimit-Limit": "100,1000",
            }
        ),
        now=now,
    )
    assert state.usage == (95, 500) and state.read_limit == (100, 1000)
    assert state.remaining_15m(now) == 5  # read quota (100) binds before overall (200)
    assert state.wait_needed_s(now) == 0
    state.record(httpx.Headers({"X-ReadRateLimit-Usage": "100,505"}), now=now + 1)
    assert state.remaining_15m(now + 1) == 0
    # budget exhausted at 10:05:01 -> wait until 10:15 + buffer
    assert state.wait_needed_s(now + 1) == pytest.approx(599 + client_mod.RATE_LIMIT_BUFFER_S)
    # once the clock passes the window boundary the quota is considered reset
    assert state.remaining_15m(_epoch(10, 15, 1)) == 100
    assert state.wait_needed_s(_epoch(10, 15, 1)) == 0
    snap = state.snapshot(now + 1)
    assert snap["usage_15m"] == 95 and snap["read_usage_15m"] == 100 and snap["remaining_15m"] == 0


def test_headers_recorded_on_success(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(api("/athlete")).mock(
        return_value=ok(
            load_fixture("athlete.json"),
            usage="10,100",
            limit="200,2000",
            **{"X-ReadRateLimit-Usage": "5,50", "X-ReadRateLimit-Limit": "100,1000"},
        )
    )
    athlete = client.get_athlete()
    assert athlete["id"] == 188844906
    assert client.rate_limit.usage == (10, 100)
    assert client.rate_limit.limit == (200, 2000)
    assert client.rate_limit.read_usage == (5, 50)
    assert client.rate_limit.remaining_15m() == 95
    assert client.rate_limit.requests_made == 1
    sent = respx_mock.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {FAKE_ACCESS}"


def test_429_waits_to_window_then_retries(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(api("/athlete")).mock(
        side_effect=[
            httpx.Response(
                429, headers={"X-RateLimit-Usage": "201,1500", "X-RateLimit-Limit": "200,2000"}
            ),
            ok({"id": 1}, usage="1,1501"),
        ]
    )
    assert client.get_athlete() == {"id": 1}
    assert route.call_count == 2
    assert len(sleeper.calls) == 1
    wait = sleeper.calls[0]
    assert (
        client_mod.MIN_RATE_LIMIT_SLEEP_S
        <= wait
        <= client_mod.WINDOW_S + client_mod.RATE_LIMIT_BUFFER_S
    )
    assert client.rate_limit.usage == (1, 1501)
    assert client.rate_limit.waits == [wait]


def test_429_honors_retry_after(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(api("/athlete")).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), ok({"id": 1})]
    )
    assert client.get_athlete() == {"id": 1}
    assert sleeper.calls == [30 + client_mod.RATE_LIMIT_BUFFER_S]


def test_429_gives_up_after_max_retries(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(api("/athlete")).mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitError, match="max retries"):
        client.get_athlete()
    assert route.call_count == client_mod.MAX_RATE_LIMIT_RETRIES + 1
    assert len(sleeper.calls) == client_mod.MAX_RATE_LIMIT_RETRIES


def test_no_wait_mode_raises_rate_limit_error(
    sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    client = StravaClient(
        lambda: FAKE_ACCESS, http=httpx.Client(), sleep=sleeper, wait_on_rate_limit=False
    )
    respx_mock.get(api("/athlete")).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(RateLimitError) as exc:
        client.get_athlete()
    assert exc.value.retry_after_s == 120 + client_mod.RATE_LIMIT_BUFFER_S
    assert sleeper.calls == []  # never slept


def test_no_wait_mode_stops_proactively_when_budget_exhausted(
    sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    client = StravaClient(
        lambda: FAKE_ACCESS, http=httpx.Client(), sleep=sleeper, wait_on_rate_limit=False
    )
    route = respx_mock.get(api("/athlete")).mock(
        return_value=ok({"id": 1}, usage="200,900", limit="200,2000")
    )
    client.get_athlete()  # headers now say the 15-min window is spent
    with pytest.raises(RateLimitError, match="budget exhausted"):
        client.get_athlete()
    assert route.call_count == 1  # second request never sent


def test_wait_mode_sleeps_proactively_when_budget_exhausted(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(api("/athlete")).mock(
        side_effect=[ok({"id": 1}, usage="200,900"), ok({"id": 2}, usage="1,901")]
    )
    client.get_athlete()
    assert client.get_athlete() == {"id": 2}
    assert route.call_count == 2 and len(sleeper.calls) == 1


def test_401_forces_refresh_once(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(api("/athlete")).mock(
        side_effect=[httpx.Response(401, json={"message": "Authorization Error"}), ok({"id": 9})]
    )
    assert client.get_athlete() == {"id": 9}
    tokens = [c.request.headers["Authorization"] for c in route.calls]
    assert tokens == [f"Bearer {FAKE_ACCESS}", "Bearer fixture-access-token-refreshed"]


def test_401_twice_is_actionable(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(api("/athlete")).mock(return_value=httpx.Response(401))
    with pytest.raises(IngestError, match="cyp auth strava"):
        client.get_athlete()


def test_5xx_backoff_then_success(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(api("/athlete")).mock(
        side_effect=[httpx.Response(502), httpx.Response(503), ok({"id": 1})]
    )
    assert client.get_athlete() == {"id": 1}
    assert sleeper.calls == [2.0, 4.0]


def test_5xx_gives_up(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(api("/athlete")).mock(return_value=httpx.Response(500))
    with pytest.raises(IngestError, match="server error 500"):
        client.get_athlete()


def test_4xx_raises_ingest_error(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(api("/activities/5")).mock(
        return_value=httpx.Response(404, json={"message": "Record Not Found"})
    )
    with pytest.raises(IngestError, match="404"):
        client.get_activity(5)


def test_transport_error_retries(
    client: StravaClient, sleeper: SleepRecorder, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(api("/athlete")).mock(side_effect=[httpx.ConnectError("boom"), ok({"id": 1})])
    assert client.get_athlete() == {"id": 1}
    assert sleeper.calls == [2.0]


def test_list_and_iter_activities_pagination(
    client: StravaClient, respx_mock: respx.MockRouter
) -> None:
    page1 = [{"id": i} for i in range(1, 4)]
    route = respx_mock.get(api("/athlete/activities")).mock(
        side_effect=lambda request: ok(
            page1
            if request.url.params["page"] == "1"
            else [{"id": 4}]
            if request.url.params["page"] == "2"
            else []
        )
    )
    got = list(client.iter_activities(after=12345, before=99999, per_page=3))
    assert [a["id"] for a in got] == [1, 2, 3, 4]
    assert route.call_count == 2  # short second page ends pagination without a third call
    first = route.calls[0].request.url.params
    assert first["after"] == "12345" and first["before"] == "99999" and first["per_page"] == "3"
    # a full page followed by an empty one -> exactly one extra call
    route.reset()
    respx_mock.get(api("/athlete/activities")).mock(
        side_effect=lambda request: ok(page1 if request.url.params["page"] == "1" else [])
    )
    assert len(list(client.iter_activities(per_page=3))) == 3
    # per_page clamp
    client.list_activities(per_page=5000)
    assert respx_mock.calls.last.request.url.params["per_page"] == "200"


def test_endpoint_paths_and_params(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    detail = respx_mock.get(api("/activities/1001")).mock(return_value=ok({"id": 1001}))
    streams = respx_mock.get(api("/activities/1001/streams")).mock(return_value=ok({}))
    zones = respx_mock.get(api("/activities/1001/zones")).mock(return_value=ok([]))
    laps = respx_mock.get(api("/activities/1001/laps")).mock(return_value=ok([{"id": 1}]))
    azones = respx_mock.get(api("/athlete/zones")).mock(return_value=ok({"power": {}}))
    client.get_activity(1001, include_all_efforts=False)
    assert detail.calls.last.request.url.params["include_all_efforts"] == "false"
    client.get_streams(1001)
    assert streams.calls.last.request.url.params["keys"] == ",".join(DEFAULT_STREAM_KEYS)
    assert streams.calls.last.request.url.params["key_by_type"] == "true"
    client.get_streams(1001, keys=["watts", "heartrate"])
    assert streams.calls.last.request.url.params["keys"] == "watts,heartrate"
    assert client.get_activity_zones(1001) == []
    assert client.get_laps(1001) == [{"id": 1}]
    assert client.get_zones() == {"power": {}}
    assert zones.called and laps.called and azones.called


def test_update_description_is_gated(client: StravaClient, respx_mock: respx.MockRouter) -> None:
    put = respx_mock.put(api("/activities/1001")).mock(return_value=ok({"id": 1001}))
    with pytest.raises(IngestError, match="writes are disabled"):
        client.update_activity_description(1001, "RIDE.LOG ...")
    assert not put.called
    writer = StravaClient(lambda: FAKE_ACCESS, http=httpx.Client(), allow_write=True)
    assert writer.update_activity_description(1001, "RIDE.LOG ...") == {"id": 1001}
    assert put.calls.last.request.content.decode() == "description=RIDE.LOG+..."
