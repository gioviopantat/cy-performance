"""StravaSyncer against a migrated SQLite DB with respx-mocked Strava endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import IngestError
from cyp.ingest.strava.client import StravaClient
from cyp.ingest.strava.sync import (
    CURSOR_AFTER,
    CURSOR_LAST_SYNC,
    CURSOR_RATE_SNAPSHOT,
    FLAG_DETAIL_FAILED,
    FLAG_STRAVA_DETAIL,
    FLAG_STREAMS_FAILED,
    SOURCE,
    StravaSyncer,
    normalize_activity,
    normalize_segment_effort,
    parse_strava_timezone,
    streams_to_frame,
)
from cyp.settings import Settings
from cyp.store.models import Athlete, StreamFile
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.job_runs import JobRunRepo
from cyp.store.repo.segments import SegmentRepo
from cyp.store.repo.sync_cursors import SyncCursorRepo
from cyp.store.repo.zones import ActivityZonesRepo
from cyp.store.runs import job_run
from cyp.store.streams import StreamStore
from tests.ingest.strava.conftest import FAKE_ACCESS, SleepRecorder, api, load_fixture, ok


@pytest.fixture
def routes(respx_mock: respx.MockRouter) -> dict[str, respx.Route]:
    """Default happy-path Strava API: 2 summaries, detail/zones/streams for 1001, 404 for 1002."""
    page = load_fixture("activities_page1.json")

    def listing(request: httpx.Request) -> httpx.Response:
        after = request.url.params.get("after")
        if request.url.params["page"] != "1":
            return ok([])
        if after and int(after) >= 1790767800:  # start of 1002
            return ok([])
        return ok(page, usage="3,300")

    detail_1002 = {**page[1], "segment_efforts": [], "laps": [], "device_name": "Rouvy"}
    return {
        "athlete": respx_mock.get(api("/athlete")).mock(
            return_value=ok(load_fixture("athlete.json"))
        ),
        "zones": respx_mock.get(api("/athlete/zones")).mock(
            return_value=ok(load_fixture("athlete_zones.json"))
        ),
        "list": respx_mock.get(api("/athlete/activities")).mock(side_effect=listing),
        "detail": respx_mock.get(api("/activities/1001")).mock(
            return_value=ok(load_fixture("activity_1001_detail.json"), usage="20,320")
        ),
        "detail2": respx_mock.get(api("/activities/1002")).mock(return_value=ok(detail_1002)),
        "azones": respx_mock.get(api("/activities/1001/zones")).mock(
            return_value=ok(load_fixture("activity_1001_zones.json"))
        ),
        "azones2": respx_mock.get(api("/activities/1002/zones")).mock(
            return_value=httpx.Response(402, json={"message": "Payment Required"})
        ),
        "streams": respx_mock.get(api("/activities/1001/streams")).mock(
            return_value=ok(load_fixture("activity_1001_streams.json"), usage="25,325")
        ),
        "streams2": respx_mock.get(api("/activities/1002/streams")).mock(
            return_value=ok({"time": {"data": [0, 1, 2]}, "watts": {"data": [100, 110, 120]}})
        ),
    }


def _syncer(
    settings: Settings, factory: sessionmaker[Session], **kwargs: Any
) -> tuple[StravaSyncer, SleepRecorder]:
    sleeper = SleepRecorder()
    client = StravaClient(
        lambda: FAKE_ACCESS,
        http=httpx.Client(),
        sleep=sleeper,
        wait_on_rate_limit=kwargs.pop("wait", True),
    )
    return StravaSyncer(settings, factory, client, **kwargs), sleeper


# ------------------------------------------------------------------------------ pure helpers


def test_parse_strava_timezone() -> None:
    assert parse_strava_timezone("(GMT+08:00) Asia/Taipei") == "Asia/Taipei"
    assert parse_strava_timezone("Europe/Paris") == "Europe/Paris"
    assert parse_strava_timezone(None) is None


def test_normalize_activity_summary_and_detail() -> None:
    summary = normalize_activity(load_fixture("activities_page1.json")[0])
    assert summary["strava_id"] == 1001
    assert summary["start_utc"] == "2026-09-28T22:00:00Z"
    assert summary["start_local"] == "2026-09-29T06:00:00+08:00"
    assert summary["tz"] == "Asia/Taipei"
    assert summary["moving_s"] == 3600 and summary["elapsed_s"] == 3700
    assert summary["has_power"] is True and summary["has_hr"] is True and summary["has_cadence"]
    assert summary["race"] is False and summary["trainer"] is False
    assert summary["np_w"] == 215.0 and summary["avg_w"] == 200.0
    assert "description" not in summary and "device_name" not in summary
    assert summary["raw_strava_json"]["map"] == {"summary_polyline": "abc"}
    race = normalize_activity(load_fixture("activities_page1.json")[1])
    assert race["race"] is True and race["trainer"] is True and race["sport_type"] == "VirtualRide"
    assert "has_cadence" not in race
    detail = normalize_activity(load_fixture("activity_1001_detail.json"))
    assert detail["description"] == "A lovely loop"
    assert detail["device_name"] == "Garmin Edge 850" and detail["gear_name"] == "Canyon Aeroad"
    assert detail["max_w"] == 600 and detail["kj"] == 720.0


def test_normalize_segment_effort_uses_internal_activity_id() -> None:
    effort = load_fixture("activity_1001_detail.json")["segment_efforts"][0]
    row = normalize_segment_effort(effort, activity_id=77)
    assert row["activity_id"] == 77 and row["segment_id"] == 5001 and row["id"] == 9001
    assert row["start_utc"] == "2026-09-28T22:10:00Z"
    assert row["pr_rank"] == 1 and "kom_rank" not in row
    assert row["achievements"] == [{"type": "pr", "rank": 1}]
    assert row["device_watts"] is True and row["elapsed_s"] == 300


def test_streams_to_frame_maps_columns_and_resamples() -> None:
    frame, meta = streams_to_frame(load_fixture("activity_1001_streams.json"))
    # time had a gap at t=3 -> forward-filled to 1 Hz
    assert frame["t_s"].to_list() == [0, 1, 2, 3, 4, 5]
    assert meta == {"original_size": 5, "resolution": "high", "resampled": True}
    assert set(frame.columns) == {
        "t_s",
        "dist_m",
        "alt_m",
        "speed_mps",
        "hr",
        "cad",
        "watts",
        "temp_c",
        "moving",
        "grade_pct",
        "lat",
        "lng",
    }
    assert frame["watts"].to_list() == [0, 180, 200, 200, 220, 215]
    assert frame["speed_mps"].to_list()[1] == pytest.approx(8.0)
    assert frame["lat"].to_list()[0] == pytest.approx(25.03)
    assert frame["moving"].to_list() == [False, True, True, True, True, True]
    contiguous, meta2 = streams_to_frame(
        {"time": {"data": [0, 1, 2]}, "watts": {"data": [1, 2, 3]}}
    )
    assert meta2["resampled"] is False and contiguous.columns == ["t_s", "watts"]
    with pytest.raises(IngestError, match="time"):
        streams_to_frame({"watts": {"data": [1]}})
    # mismatched lengths are dropped rather than corrupting the frame
    frame3, _ = streams_to_frame({"time": {"data": [0, 1]}, "heartrate": {"data": [1]}})
    assert frame3.columns == ["t_s"]


# ------------------------------------------------------------------------------------ syncer


def test_disabled_is_a_noop(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    settings = strava_settings.model_copy(update={"strava_enabled": False})
    syncer, _ = _syncer(settings, factory)
    summary = syncer.run()
    assert summary.skipped is True and summary.counts()["activities_seen"] == 0
    assert not any(r.called for r in routes.values())
    with factory() as s:
        assert ActivityRepo(s).count() == 0
        assert SyncCursorRepo(s).all_for(SOURCE) == []


def test_full_pipeline_persists_everything(
    strava_settings: Settings,
    factory: sessionmaker[Session],
    routes: dict[str, respx.Route],
    data_dir: Path,
) -> None:
    syncer, sleeper = _syncer(strava_settings, factory, fetch_streams=True)
    with job_run("sync", factory) as ctx:
        summary = syncer.run(ctx)

    assert len(summary.errors) == 1 and summary.errors[0].startswith("zones 1002: Strava 402")
    assert summary.activities_seen == 2 and summary.activities_new == 2
    assert summary.details_fetched == 2 and summary.details_remaining == 0
    assert summary.segments == 2 and summary.efforts == 2 and summary.laps == 2
    assert summary.zones_fetched == 1 and summary.streams_fetched == 2
    assert summary.rate_limited is False
    assert summary.cursor_before is None and summary.cursor_after == 1790767800
    assert sleeper.calls == []

    with factory() as s:
        repo = ActivityRepo(s)
        a = repo.get_by_strava_id(1001)
        assert a is not None
        assert a.match_method == "single_source" and a.is_ride is True
        assert summary.activities_joined_icu == 0
        assert a.description == "A lovely loop" and a.device_name == "Garmin Edge 850"
        assert a.gear_name == "Canyon Aeroad" and a.np_w == 215.0
        assert a.start_utc == "2026-09-28T22:00:00Z" and a.tz == "Asia/Taipei"
        assert a.pending_detail is False and a.pending_streams is False
        assert a.stage_flags == {
            "detail": True,
            "efforts": True,
            "strava_detail": True,
            "zones": True,
            "streams": True,
        }
        assert a.raw_strava_json is not None
        assert [lap["id"] for lap in a.raw_strava_json["laps"]] == [8001, 8002]  # laps as raw
        athlete = s.scalars(select(Athlete)).one()
        assert athlete.strava_id == 188844906 and athlete.name == "Test Rider"
        assert athlete.weight_kg == 70.0
        assert athlete.raw_strava_json is not None and "power" in athlete.raw_strava_json["zones"]
        assert a.athlete_id == athlete.id

        srepo = SegmentRepo(s)
        seg = srepo.get_segment(5001)
        assert seg is not None and seg.name == "Hill Climb" and seg.city == "Taipei"
        assert seg.starred is True and seg.distance_m == 1200.0
        efforts = srepo.efforts_for_activity(a.id)
        assert [e.id for e in efforts] == [9001, 9002]
        assert efforts[0].pr_rank == 1 and efforts[0].achievements == [{"type": "pr", "rank": 1}]
        assert efforts[0].segment_id == 5001 and efforts[0].avg_w == 310.0
        assert [e.id for e in srepo.efforts_for_segment(5002)] == [9002]
        assert srepo.count_segments() == 2

        zones = ActivityZonesRepo(s).for_activity(a.id)
        assert [z.zone_type for z in zones] == ["heartrate", "power"]
        hr = ActivityZonesRepo(s).get(a.id, "heartrate")
        assert hr is not None and hr.points == 42 and hr.sensor_based is True
        assert hr.distribution_buckets[1]["time"] == 1800

        sf = s.get(StreamFile, a.id)
        assert sf is not None and sf.source == "strava" and sf.n_samples == 6 and sf.hz == 1.0
        assert sf.original_size == 5 and sf.resolution == "high"
        assert sf.raw_json_path is not None and Path(sf.raw_json_path).is_file()  # resampled
        assert "watts" in sf.columns and "lat" in sf.columns

        b = repo.get_by_strava_id(1002)
        assert b is not None and b.device_name == "Rouvy" and b.race is True
        assert b.stage_flags == {
            "detail": True,
            "efforts": True,
            "strava_detail": True,
            "zones": True,
            "streams": True,
        }
        sf2 = s.get(StreamFile, b.id)
        assert sf2 is not None and sf2.raw_json_path is None  # contiguous: no raw copy

        cursors = SyncCursorRepo(s)
        assert cursors.get(SOURCE, CURSOR_AFTER) == "1790767800"
        assert cursors.get(SOURCE, CURSOR_LAST_SYNC) is not None
        snap = json.loads(cursors.get(SOURCE, CURSOR_RATE_SNAPSHOT) or "{}")
        assert snap["usage_15m"] == 10 and snap["limit_15m"] == 200 and snap["remaining_15m"] == 190

        run = JobRunRepo(s).latest("sync")
        assert run is not None and run.status == "ok"
        assert run.counts["details_fetched"] == 2 and run.counts["errors"] == 1
        assert run.rate_limit_snapshot["usage_15m"] == 10

    store = StreamStore(strava_settings.streams_dir)
    frame = store.read(a.id)
    assert frame.schema["watts"] == pl.Int16 and frame.height == 6
    assert sorted(store.list_ids()) == sorted([a.id, b.id])
    assert routes["streams"].call_count == 1 and routes["azones2"].call_count == 1


def test_second_run_is_incremental_and_streams_skip_existing_parquet(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    syncer, _ = _syncer(strava_settings, factory, fetch_streams=False)
    first = syncer.run()
    assert first.streams_fetched == 0 and not routes["streams"].called
    with factory() as s:
        a = ActivityRepo(s).get_by_strava_id(1001)
        assert a is not None and a.pending_streams is True  # left for the icu sync
        b = ActivityRepo(s).get_by_strava_id(1002)
        assert b is not None
        internal_a, internal_b = a.id, b.id
    # simulate intervals.icu having written the Parquet for 1001 (primary stream source)
    store = StreamStore(strava_settings.streams_dir)
    store.write(internal_a, pl.DataFrame({"t_s": [0, 1], "watts": [1, 2]}))

    syncer2, _ = _syncer(strava_settings, factory, fetch_streams=True)
    second = syncer2.run()
    assert second.cursor_before == 1790767800 and second.cursor_after == 1790767800
    assert second.activities_seen == 0 and second.details_fetched == 0  # nothing pending
    # detail already done -> the *fallback* stage still fetches 1002 (no streams anywhere)
    # and skips 1001 because icu's Parquet exists
    assert second.streams_fallback_fetched == 1 and second.streams_fetched == 1
    assert second.streams_skipped_existing == 1 and second.streams_fallback_remaining == 0
    assert not routes["streams"].called and routes["streams2"].call_count == 1
    listing_after = routes["list"].calls.last.request.url.params["after"]
    assert listing_after == "1790767800"
    # a forced re-detail (pending flag reset) fetches nothing more: both have streams
    with factory() as s:
        for sid in (1001, 1002):
            row = ActivityRepo(s).get_by_strava_id(sid)
            assert row is not None
            row.pending_detail = True
        s.commit()
    third = syncer2.run()
    assert third.streams_fetched == 0 and third.streams_fallback_fetched == 0
    assert not routes["streams"].called and routes["streams2"].call_count == 1
    with factory() as s:
        assert s.get(StreamFile, internal_a) is None  # we never claimed icu's file
        sf = s.get(StreamFile, internal_b)
        assert sf is not None and sf.source == "strava"


def test_stream_fallback_covers_icu_rows_within_budget(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    """icu rows that carry a strava_id but no streams (Strava-origin) get Strava streams."""
    with factory() as s:
        repo = ActivityRepo(s)
        for sid, start in ((1001, "2026-09-28T22:00:00Z"), (1002, "2026-09-30T11:30:00Z")):
            repo.upsert(
                {
                    "strava_id": sid,
                    "intervals_id": str(sid),
                    "match_method": "strava_id",
                    "sport_type": "VirtualRide",
                    "start_utc": start,
                    "pending_detail": False,  # icu already "did" detail: not in the queue
                    "pending_streams": False,
                    # Strava detail done too: keep the efforts fallback out of this budget
                    "stage_flags": {
                        "detail": True,
                        FLAG_STRAVA_DETAIL: True,
                        "streams_skipped": "strava_origin",
                    },
                }
            )
        repo.upsert(  # a non-ride with a strava id must not consume budget
            {
                "strava_id": 1003,
                "intervals_id": "1003",
                "sport_type": "Yoga",
                "start_utc": "2026-09-30T12:00:00Z",
                "pending_detail": False,
            }
        )
        s.commit()
    # budget 1: one fallback fetch per run, newest first; without --streams nothing happens
    off, _ = _syncer(strava_settings, factory, fetch_streams=False, max_detail_fetches=1)
    assert off.run().streams_fallback_fetched == 0
    syncer, _ = _syncer(strava_settings, factory, fetch_streams=True, max_detail_fetches=1)
    first = syncer.run()
    assert first.details_fetched == 0  # nothing pending_detail
    assert first.streams_fallback_fetched == 1 and first.streams_fallback_remaining == 1
    assert routes["streams2"].call_count == 1 and not routes["streams"].called  # newest first
    second = syncer.run()
    assert second.streams_fallback_fetched == 1 and second.streams_fallback_remaining == 0
    assert routes["streams"].call_count == 1
    third = syncer.run()
    assert third.streams_fallback_fetched == 0 and third.streams_fallback_remaining == 0
    with factory() as s:
        for sid in (1001, 1002):
            row = ActivityRepo(s).get_by_strava_id(sid)
            assert row is not None
            sf = s.get(StreamFile, row.id)
            assert sf is not None and sf.source == "strava"
            assert row.stage_flags is not None and row.stage_flags["streams"] is True
        yoga = ActivityRepo(s).get_by_strava_id(1003)
        assert yoga is not None and s.get(StreamFile, yoga.id) is None
    assert third.streams_skipped_existing == 0  # nothing re-queued once stream_files exists


def test_stream_fallback_rate_limit_ends_run_cleanly(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    with factory() as s:
        ActivityRepo(s).upsert(
            {
                "strava_id": 1001,
                "intervals_id": "1001",
                "sport_type": "Ride",
                "start_utc": "2026-09-28T22:00:00Z",
                "pending_detail": False,
            }
        )
        s.commit()
    routes["streams"].mock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
    syncer, sleeper = _syncer(strava_settings, factory, fetch_streams=True, wait=False)
    summary = syncer.run()
    assert summary.rate_limited is True and summary.streams_fallback_fetched == 0
    assert summary.streams_fallback_remaining == 1 and sleeper.calls == []
    assert routes["streams"].call_count == 1
    with factory() as s:  # a 429 is transient: never negative-cached
        row = ActivityRepo(s).get_by_strava_id(1001)
        assert row is not None and summary.fetch_failures_cached == 0
        assert FLAG_STREAMS_FAILED not in (row.stage_flags or {})


def test_existing_row_keeps_match_method(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    with factory() as s:
        ActivityRepo(s).upsert(
            {
                "strava_id": 1001,
                "intervals_id": "i1",
                "match_method": "strava_id",
                "sport_type": "Ride",
                "start_utc": "2026-09-28T22:00:00Z",
                "moving_s": 1,
                "elapsed_s": 1,
            }
        )
        s.commit()
    syncer, _ = _syncer(strava_settings, factory)
    summary = syncer.run()
    assert summary.activities_seen == 2 and summary.activities_new == 1
    with factory() as s:
        a = ActivityRepo(s).get_by_strava_id(1001)
        assert a is not None and a.match_method == "strava_id" and a.intervals_id == "i1"
        assert a.description == "A lovely loop"  # enriched from detail


def test_detail_budget_caps_fetches(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    syncer, _ = _syncer(strava_settings, factory, max_detail_fetches=1)
    summary = syncer.run()
    assert summary.details_fetched == 1 and summary.details_remaining == 1
    assert routes["detail"].call_count == 1 and routes["detail2"].call_count == 0  # oldest first
    assert summary.cursor_after == 1790767800  # cursor still advanced: listing completed
    with factory() as s:
        pending = [a.strava_id for a in ActivityRepo(s).list_pending("detail")]
        assert pending == [1002]
    again = syncer.run()
    assert again.details_fetched == 1 and again.details_remaining == 0
    assert routes["detail2"].call_count == 1


def test_no_wait_rate_limit_stops_cleanly_with_cursor_persisted(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    routes["detail"].mock(return_value=httpx.Response(429, headers={"Retry-After": "60"}))
    syncer, sleeper = _syncer(strava_settings, factory, wait=False)
    with job_run("sync", factory) as ctx:
        summary = syncer.run(ctx)
    assert summary.rate_limited is True
    assert summary.errors and summary.errors[-1].startswith("rate_limit:")
    assert summary.details_fetched == 0 and summary.details_remaining == 2
    assert sleeper.calls == []
    assert routes["detail2"].call_count == 0  # stopped at the first 429
    with factory() as s:
        assert SyncCursorRepo(s).get(SOURCE, CURSOR_AFTER) == "1790767800"
        assert ActivityRepo(s).count() == 2
        run = JobRunRepo(s).latest("sync")
        assert run is not None and run.status == "ok" and run.counts["errors"] == 1
    # the next run resumes from the persisted cursor and finishes the details
    routes["detail"].mock(return_value=ok(load_fixture("activity_1001_detail.json")))
    resumed, _ = _syncer(strava_settings, factory, wait=False)
    summary2 = resumed.run()
    assert summary2.rate_limited is False and summary2.details_fetched == 2
    assert routes["list"].calls.last.request.url.params["after"] == "1790767800"


def test_listing_failure_does_not_advance_cursor(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    page = load_fixture("activities_page1.json")
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return ok(page[:1] * 200, usage="1,1")  # a full page -> client asks for page 2
        return httpx.Response(404, json={"message": "gone"})

    routes["list"].mock(side_effect=flaky)
    syncer, _ = _syncer(strava_settings, factory)
    summary = syncer.run()
    assert any(e.startswith("list_activities:") for e in summary.errors)
    assert summary.cursor_after is None
    with factory() as s:
        assert SyncCursorRepo(s).get(SOURCE, CURSOR_AFTER) is None
        assert ActivityRepo(s).count() == 1  # what was seen is still persisted
    assert summary.details_fetched == 1  # detail stage still runs on what we have


def test_listing_rate_limit_in_no_wait_mode_keeps_old_cursor(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    with factory() as s:
        SyncCursorRepo(s).set(SOURCE, CURSOR_AFTER, "1000")
        s.commit()
    routes["list"].mock(return_value=httpx.Response(429))
    syncer, _ = _syncer(strava_settings, factory, wait=False)
    summary = syncer.run()
    assert summary.rate_limited and summary.cursor_before == 1000 and summary.cursor_after is None
    with factory() as s:
        assert SyncCursorRepo(s).get(SOURCE, CURSOR_AFTER) == "1000"


def test_full_flag_ignores_cursor(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    with factory() as s:
        SyncCursorRepo(s).set(SOURCE, CURSOR_AFTER, "1790767800")
        s.commit()
    syncer, _ = _syncer(strava_settings, factory)
    summary = syncer.run(full=True)
    assert "after" not in routes["list"].calls[0].request.url.params
    assert summary.activities_seen == 2 and summary.cursor_before is None


def test_athlete_failure_is_recorded_not_fatal(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    routes["athlete"].mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    syncer, _ = _syncer(strava_settings, factory)
    summary = syncer.run()
    assert any(e.startswith("athlete:") for e in summary.errors)
    assert summary.activities_seen == 2 and summary.athlete_id is None
    with factory() as s:
        assert s.scalars(select(Athlete)).first() is None


# ------------------------------------------------------------------- efforts fallback (icu rows)

_ICU_FLAGS = {"detail": True, "streams": True, "intervals": True}


def _seed_icu_rows(factory: sessionmaker[Session], *, pending_1001: bool = False) -> None:
    """Two icu rides carrying a strava_id (icu already 'did' detail) plus a non-ride.

    The listing cursor is parked at the newest fixture start so the summary stage lists nothing
    and the rows below reach the fallback untouched.
    """
    with factory() as s:
        repo = ActivityRepo(s)
        for sid, start in ((1001, "2026-09-28T22:00:00Z"), (1002, "2026-09-30T11:30:00Z")):
            repo.upsert(
                {
                    "strava_id": sid,
                    "intervals_id": f"i{sid}",
                    "match_method": "strava_id",
                    "sport_type": "Ride",
                    "name": f"icu name {sid}",
                    "distance_m": 12345.0,
                    "start_utc": start,
                    "pending_detail": pending_1001 and sid == 1001,
                    "pending_streams": False,
                    "stage_flags": dict(_ICU_FLAGS),
                }
            )
        repo.upsert(
            {
                "strava_id": 1003,
                "intervals_id": "i1003",
                "sport_type": "Yoga",
                "start_utc": "2026-09-30T12:00:00Z",
                "pending_detail": False,
                "stage_flags": dict(_ICU_FLAGS),
            }
        )
        SyncCursorRepo(s).set(SOURCE, CURSOR_AFTER, "1790767800")
        s.commit()


def test_efforts_fallback_fetches_detail_for_icu_rows(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    _seed_icu_rows(factory)
    capped, _ = _syncer(strava_settings, factory, max_detail_fetches=1)
    first = capped.run()
    assert first.activities_seen == 0 and first.details_fetched == 0  # nothing pending
    assert first.efforts_fallback_fetched == 1 and first.efforts_fallback_remaining == 1
    assert routes["detail2"].call_count == 1 and not routes["detail"].called  # newest first
    assert any(e.startswith("zones 1002: Strava 402") for e in first.errors)

    syncer, _ = _syncer(strava_settings, factory)
    summary = syncer.run()
    assert summary.efforts_fallback_fetched == 1 and summary.efforts_fallback_remaining == 0
    assert summary.segments == 2 and summary.efforts == 2 and summary.laps == 2
    assert summary.zones_fetched == 1 and summary.errors == []
    assert routes["detail"].call_count == 1 and routes["detail2"].call_count == 1
    assert routes["detail"].calls[0].request.url.params["include_all_efforts"] == "true"
    assert not routes["streams"].called and not routes["streams2"].called

    with factory() as s:
        repo = ActivityRepo(s)
        a = repo.get_by_strava_id(1001)
        assert a is not None and a.intervals_id == "i1001"
        # icu's columns stay; Strava fills only what icu left empty + the raw payload
        assert a.name == "icu name 1001" and a.distance_m == 12345.0
        assert a.description == "A lovely loop" and a.device_name == "Garmin Edge 850"
        assert a.raw_strava_json is not None and len(a.raw_strava_json["laps"]) == 2
        # icu's detail semantics untouched; Strava's own marker added
        assert a.pending_detail is False and a.stage_flags is not None
        assert a.stage_flags["detail"] is True and a.stage_flags["intervals"] is True
        assert a.stage_flags[FLAG_STRAVA_DETAIL] is True and a.stage_flags["efforts"] is True
        assert a.stage_flags["zones"] is True and a.pending_analysis is True
        srepo = SegmentRepo(s)
        assert [e.id for e in srepo.efforts_for_activity(a.id)] == [9001, 9002]
        assert srepo.count_segments() == 2
        assert [z.zone_type for z in ActivityZonesRepo(s).for_activity(a.id)] == [
            "heartrate",
            "power",
        ]
        b = repo.get_by_strava_id(1002)
        assert b is not None and b.stage_flags is not None
        assert b.stage_flags[FLAG_STRAVA_DETAIL] is True and b.device_name == "Rouvy"
        assert srepo.efforts_for_activity(b.id) == []
        yoga = repo.get_by_strava_id(1003)
        assert yoga is not None and FLAG_STRAVA_DETAIL not in (yoga.stage_flags or {})

    again = syncer.run()  # idempotent: nothing left to fetch
    assert again.efforts_fallback_fetched == 0 and again.efforts_fallback_remaining == 0
    assert routes["detail"].call_count == 1 and routes["detail2"].call_count == 1


def test_efforts_fallback_shares_budget_with_detail_queue_and_stream_fallback(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    _seed_icu_rows(factory, pending_1001=True)
    # budget 1: the pending detail wins, the fallback gets nothing
    syncer, _ = _syncer(strava_settings, factory, fetch_streams=True, max_detail_fetches=1)
    first = syncer.run()
    assert first.details_fetched == 1 and routes["detail"].call_count == 1
    assert first.efforts_fallback_fetched == 0 and first.efforts_fallback_remaining == 1
    assert first.streams_fallback_fetched == 0 and first.streams_fallback_remaining == 1
    assert routes["streams"].call_count == 1  # detail path's own stream fetch (uncounted)
    assert not routes["detail2"].called and not routes["streams2"].called
    # budget 1 again: the efforts fallback goes before the stream fallback
    second = syncer.run()
    assert second.details_fetched == 0 and second.efforts_fallback_fetched == 1
    assert second.streams_fallback_fetched == 0 and second.streams_fallback_remaining == 1
    assert routes["detail2"].call_count == 1 and not routes["streams2"].called
    # budget 1, nothing else owed: the stream fallback finally runs
    third = syncer.run()
    assert third.efforts_fallback_fetched == 0 and third.streams_fallback_fetched == 1
    assert routes["streams2"].call_count == 1
    with factory() as s:
        for sid in (1001, 1002):
            row = ActivityRepo(s).get_by_strava_id(sid)
            assert row is not None and row.stage_flags is not None
            assert row.stage_flags[FLAG_STRAVA_DETAIL] is True
            assert s.get(StreamFile, row.id) is not None


def test_efforts_fallback_negative_cache_set_skip_and_clear_on_full(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    _seed_icu_rows(factory)
    routes["detail2"].mock(return_value=httpx.Response(404, json={"message": "Record Not Found"}))
    syncer, _ = _syncer(strava_settings, factory)
    first = syncer.run()
    assert first.efforts_fallback_fetched == 1 and first.efforts_fallback_remaining == 0
    assert first.fetch_failures_cached == 1
    assert any(e.startswith("efforts_fallback") and "404" in e for e in first.errors)
    assert routes["detail2"].call_count == 1 and routes["detail"].call_count == 1
    with factory() as s:
        b = ActivityRepo(s).get_by_strava_id(1002)
        assert b is not None and b.stage_flags is not None
        marker = b.stage_flags[FLAG_DETAIL_FAILED]
        assert marker.endswith(" 404") and marker.startswith("20")  # "<iso ts> 404"
        assert FLAG_STRAVA_DETAIL not in b.stage_flags and b.stage_flags["detail"] is True
        assert b.pending_detail is False

    second = syncer.run()  # negative-cached row is not a candidate any more
    assert second.efforts_fallback_fetched == 0 and second.efforts_fallback_remaining == 0
    assert second.fetch_failures_cached == 0 and routes["detail2"].call_count == 1

    routes["detail2"].mock(return_value=ok({**load_fixture("activities_page1.json")[1]}))
    third = syncer.run(full=True)  # --full forgets the marker and retries
    assert third.negative_cache_cleared == 1 and third.efforts_fallback_fetched == 1
    assert routes["detail2"].call_count == 2
    with factory() as s:
        b = ActivityRepo(s).get_by_strava_id(1002)
        assert b is not None and b.stage_flags is not None
        assert FLAG_DETAIL_FAILED not in b.stage_flags
        assert b.stage_flags[FLAG_STRAVA_DETAIL] is True


def test_stream_fallback_negative_cache_set_skip_and_clear_on_full(
    strava_settings: Settings, factory: sessionmaker[Session], routes: dict[str, respx.Route]
) -> None:
    _seed_icu_rows(factory)
    for sid in (1001, 1002):  # pretend the efforts fallback already ran: isolate streams
        with factory() as s:
            row = ActivityRepo(s).get_by_strava_id(sid)
            assert row is not None
            row.stage_flags = {**_ICU_FLAGS, FLAG_STRAVA_DETAIL: True, "efforts": True}
            s.commit()
    routes["streams2"].mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    syncer, _ = _syncer(strava_settings, factory, fetch_streams=True)
    first = syncer.run()
    assert first.efforts_fallback_fetched == 0
    assert first.streams_fallback_fetched == 1 and first.streams_fallback_remaining == 0
    assert first.fetch_failures_cached == 1 and routes["streams2"].call_count == 1
    assert any(e.startswith("streams 1002: Strava 403") for e in first.errors)
    with factory() as s:
        b = ActivityRepo(s).get_by_strava_id(1002)
        assert b is not None and b.stage_flags is not None
        assert b.stage_flags[FLAG_STREAMS_FAILED].endswith(" 403")
        assert s.get(StreamFile, b.id) is None

    second = syncer.run()
    assert second.streams_fallback_remaining == 0 and routes["streams2"].call_count == 1

    routes["streams2"].mock(
        return_value=ok({"time": {"data": [0, 1, 2]}, "watts": {"data": [100, 110, 120]}})
    )
    third = syncer.run(full=True)
    assert third.negative_cache_cleared == 1 and third.streams_fallback_fetched == 1
    assert routes["streams2"].call_count == 2
    with factory() as s:
        b = ActivityRepo(s).get_by_strava_id(1002)
        assert b is not None and b.stage_flags is not None
        assert FLAG_STREAMS_FAILED not in b.stage_flags and b.stage_flags["streams"] is True
        assert s.get(StreamFile, b.id) is not None
