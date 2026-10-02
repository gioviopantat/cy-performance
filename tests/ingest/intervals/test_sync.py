"""IntervalsSyncer stages, cursors/overlap, stream handling, backfill paging + resume."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import polars as pl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import IngestError
from cyp.ingest.intervals.client import IntervalsClient
from cyp.ingest.intervals.sync import (
    CURSOR_ACTIVITIES,
    CURSOR_ATHLETE_ID,
    CURSOR_BACKFILL_PROGRESS,
    CURSOR_WELLNESS,
    SOURCE,
    IntervalsSyncer,
    SyncOptions,
)
from cyp.store.models import Athlete, IcuEvent, JobRun
from cyp.store.repo import (
    ActivityIntervalRepo,
    ActivityRepo,
    AthleteSettingsRepo,
    IcuEventRepo,
    JobRunRepo,
    PowerCurveRepo,
    SyncCursorRepo,
    WellnessRepo,
)
from cyp.store.streams import StreamStore
from tests.ingest.intervals.conftest import ATHLETE_ID, NOW_LOCAL, TODAY, FakeIcu, ok


def _cursor(factory: sessionmaker[Session], key: str) -> str | None:
    with factory() as s:
        return SyncCursorRepo(s).get(SOURCE, key)


def _athlete_pk(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        row = AthleteSettingsRepo(s).get_by_intervals_id(ATHLETE_ID)
        assert row is not None
        return row.id


# ----------------------------------------------------------------------------- athlete stage


def test_athlete_stage_upserts_athlete_and_settings_history(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    report = syncer.run(("athlete",))
    assert report.stages["athlete"] == {"athlete": 1, "settings_history_added": 1}
    assert _cursor(factory, CURSOR_ATHLETE_ID) == ATHLETE_ID
    with factory() as s:
        athlete = s.scalars(select(Athlete)).one()
        assert athlete.intervals_id == ATHLETE_ID
        assert athlete.strava_id == 188844906
        assert athlete.name == "Roger C" and athlete.timezone == "Asia/Taipei"
        assert athlete.weight_kg == 64.0
        assert (
            athlete.raw_intervals_json is not None
            and athlete.raw_intervals_json["id"] == ATHLETE_ID
        )
        hist = AthleteSettingsRepo(s).history(athlete.id)
        assert len(hist) == 1
        row = hist[0]
        assert row.source == "icu_sport_settings"
        assert row.effective_from == TODAY
        assert (row.ftp, row.indoor_ftp, row.eftp, row.w_prime, row.p_max) == (
            250,
            245,
            258,
            20000,
            1050,
        )
        assert (row.lthr, row.max_hr, row.resting_hr, row.weight_kg) == (165, 190, 48, 64.0)
        assert row.power_zones is not None and row.power_zones["anchor"] == 250
        z = row.power_zones["zones"]
        assert [zz["name"] for zz in z] == ["Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7"]
        assert z[0] == {"idx": 1, "name": "Z1", "lo": 0.0, "hi": 137.5}
        assert z[3]["lo"] == 225.0 and z[3]["hi"] == 262.5
        assert z[-1]["hi"] is None  # 999 = open-ended
        assert row.hr_zones is not None and row.hr_zones["anchor"] == 165
        assert row.hr_zones["zones"][1] == {"idx": 2, "name": "Z2", "lo": 133.0, "hi": 149.0}
        assert row.raw_json is not None and row.raw_json["sport_settings"]["id"] == 101
        run = JobRunRepo(s).latest("sync:icu:athlete")
        assert run is not None and run.status == "ok"
        assert run.rate_limit_snapshot == {
            "intervals_limit": 2500,
            "intervals_remaining": 2487,
            "intervals_retry_after_s": None,
            "intervals_observed_at": run.rate_limit_snapshot["intervals_observed_at"],
            "intervals_429_hits": 0,
        }


def test_settings_history_grows_only_when_values_change(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    syncer.run(("athlete",))
    second = syncer.run(("athlete",))
    assert "settings_history_added" not in second.stages["athlete"]
    icu.sport_settings[0]["ftp"] = 262  # the athlete bumped FTP on icu
    icu.sport_settings[0]["power_zones"] = [55, 75, 90, 105, 120, 150, 999]
    third = syncer.run(("athlete",))
    assert third.stages["athlete"]["settings_history_added"] == 1
    with factory() as s:
        hist = AthleteSettingsRepo(s).history(_athlete_pk(factory))
        assert [h.ftp for h in hist] == [250, 262]
        assert hist[1].power_zones is not None and hist[1].power_zones["zones"][3]["hi"] == 275.1
        assert s.scalars(select(Athlete)).one().id == hist[0].athlete_id  # still one athlete


# -------------------------------------------------------------------------- activities stage


def test_activities_stage_stores_icu_fields_streams_and_intervals(
    icu: FakeIcu,
    syncer: IntervalsSyncer,
    factory: sessionmaker[Session],
    stream_store: StreamStore,
) -> None:
    report = syncer.run(("activities",))
    counts = report.stages["activities"]
    assert counts["activities_listed"] == 3
    assert counts["activities_new"] == 3
    assert counts["streams_written"] == 1
    assert counts["stream_samples"] == 780
    assert counts["streams_skipped_strava"] == 1
    assert counts["streams_skipped_none"] == 1  # WeightTraining: not a ride
    assert counts["intervals_activities"] == 1 and counts["intervals_rows"] == 4
    assert icu.r_streams.call_count == 1  # only the Garmin ride
    assert icu.r_intervals.call_count == 1
    assert _cursor(factory, CURSOR_ACTIVITIES) == TODAY.isoformat()
    assert _cursor(factory, CURSOR_ATHLETE_ID) == ATHLETE_ID  # implicit athlete stage ran

    with factory() as s:
        repo = ActivityRepo(s)
        assert repo.count() == 3
        ride = repo.get_by_intervals_id("i3001")
        assert ride is not None
        assert ride.athlete_id == _athlete_pk(factory)
        assert ride.is_ride is True and ride.sport_type == "Ride"
        assert ride.start_utc == "2026-09-29T22:12:34Z"
        assert ride.start_local == "2026-09-30T06:12:34+08:00" and ride.tz == "Asia/Taipei"
        assert (ride.moving_s, ride.elapsed_s) == (780, 812)
        assert ride.distance_m == 6400.5 and ride.elev_gain_m == 48.2
        assert ride.has_power and ride.has_hr and ride.has_cadence
        assert ride.device_name == "Garmin Edge 850"
        assert ride.gear_id == "b1234567" and ride.gear_name == "Canyon Endurace"
        assert (ride.avg_w, ride.np_w, ride.kj) == (209, 228, 163.2)
        assert (ride.avg_hr, ride.max_hr, ride.avg_cad) == (146, 171, 88.4)
        assert ride.icu_training_load == 21 and ride.icu_intensity == 91.2
        assert ride.icu_ftp == 250 and ride.icu_eftp == 258  # icu_pm_ftp -> icu_eftp
        assert (ride.icu_pm_cp, ride.icu_pm_w_prime, ride.icu_pm_p_max) == (262, 19500, 1040)
        assert ride.icu_decoupling == 3.4 and ride.icu_polarization_index == 1.82
        assert ride.icu_joules_above_ftp == 12300 and ride.icu_max_wbal_depletion == 6800
        assert ride.icu_zone_times == [120, 200, 60, 300, 100, 0, 0]  # flattened {id,secs}
        assert ride.icu_hr_zone_times == [150, 220, 110, 250, 50, 0]
        assert ride.paired_event_id == 90001 and ride.icu_rpe == 7 and ride.feel == 4
        assert ride.raw_intervals_json == icu.ride()
        assert ride.pending_detail is False and ride.pending_streams is False
        assert ride.pending_analysis is True
        assert ride.stage_flags == {"detail": True, "streams": True, "intervals": True}

        sf = repo.get_stream_file(ride.id)
        assert sf is not None
        assert sf.source == "intervals" and sf.hz == 1.0
        assert sf.original_size == 600 and sf.n_samples == 780 and sf.resolution == "1s"
        assert Path(sf.path) == stream_store.path_for(ride.id)
        assert sf.raw_json_path is not None and Path(sf.raw_json_path).is_file()
        assert json.loads(Path(sf.raw_json_path).read_text())[0]["type"] == "time"
        frame = stream_store.read(ride.id)
        assert frame.height == 780 and "watts" in frame.columns and "lat" in frame.columns

        ivs = ActivityIntervalRepo(s).list_for(ride.id, "icu")
        assert [i.label for i in ivs] == ["Warmup", "Threshold 1", "Recovery", "Threshold 2"]
        assert [i.type for i in ivs] == ["WORK", "WORK", "RECOVERY", "WORK"]
        assert (ivs[1].start_s, ivs[1].duration_s) == (331, 299)
        assert ivs[1].avg_w == 266 and ivs[1].np_w == 270 and ivs[1].zone == 4
        assert ivs[1].wbal_start == 15400 and ivs[1].raw_json is not None

        strava = repo.get_by_intervals_id("i3002")
        assert strava is not None
        assert strava.strava_id == 12345678901 and strava.trainer is True
        assert strava.match_method == "strava_id"  # icu carried the Strava id
        assert strava.sport_type == "VirtualRide" and strava.is_ride is True
        assert strava.pending_streams is False and not stream_store.exists(strava.id)
        assert strava.stage_flags == {
            "detail": True,
            "streams": False,
            "streams_skipped": "strava_origin",
        }
        assert ActivityIntervalRepo(s).count_for(strava.id) == 0

        gym = repo.get_by_intervals_id("i3003")
        assert gym is not None and gym.is_ride is False and gym.pending_streams is False
        assert gym.icu_training_load == 25 and gym.start_utc == "2026-09-29T11:05:00Z"
        assert gym.stage_flags is not None and gym.stage_flags["streams_skipped"] == "no_streams"


def test_rerun_is_idempotent_and_edits_refresh_without_refetching_streams(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    syncer.run(("activities",))
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None
        ride.pending_analysis = False
        s.commit()
    second = syncer.run(("activities",))
    # Cursor is now "today", so the overlap window (today - 3 d) no longer lists the 09-28 ride.
    assert second.stages["activities"]["activities_listed"] == 2
    assert second.stages["activities"]["activities_unchanged"] == 2
    assert "activities_updated" not in second.stages["activities"]
    assert icu.r_streams.call_count == 1 and icu.r_intervals.call_count == 1
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None and ride.pending_analysis is False and ride.updated_at is not None

    icu.ride()["name"] = "Renamed on icu"
    icu.ride()["icu_rpe"] = 8
    third = syncer.run(("activities",))
    assert third.stages["activities"]["activities_updated"] == 1
    assert icu.r_streams.call_count == 1  # streams unchanged: not re-downloaded
    assert icu.r_intervals.call_count == 2  # intervals may have been edited: refreshed
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None
        assert ride.name == "Renamed on icu" and ride.icu_rpe == 8
        assert ride.pending_analysis is True  # edited -> re-analyse
        assert ActivityRepo(s).count() == 3


def test_refetch_streams_rewrites_icu_files_with_latlng(
    icu: FakeIcu,
    syncer: IntervalsSyncer,
    factory: sessionmaker[Session],
    stream_store: StreamStore,
) -> None:
    syncer.run(("activities",))
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None
        ride_pk = ride.id
        ride.pending_analysis = False
        s.commit()
    # Simulate a file written by the old (buggy) mapping: lat/lng all null.
    stale = stream_store.read(ride_pk).with_columns(
        pl.lit(None, dtype=pl.Float64).alias("lat"), pl.lit(None, dtype=pl.Float64).alias("lng")
    )
    stream_store.write(ride_pk, stale)

    counts = syncer.refetch_streams()  # default: every stream_files.source == 'intervals'
    assert counts["streams_written"] == 1
    assert icu.r_streams.call_count == 2
    frame = stream_store.read(ride_pk)
    assert frame["lat"].drop_nulls().len() > 0 and frame["lng"].drop_nulls().len() > 0
    with factory() as s:
        ride = ActivityRepo(s).get(ride_pk)
        assert ride is not None and ride.pending_analysis is True
        assert s.scalars(select(JobRun).where(JobRun.job == "sync:icu:refetch_streams")).one()


def test_cursor_overlap_window(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    syncer.run(("athlete",))
    with factory() as s:
        SyncCursorRepo(s).set(SOURCE, CURSOR_ACTIVITIES, "2026-10-01")
        s.commit()
    syncer.run(("activities",))
    assert icu.activity_requests() == [("2026-09-28", "2026-10-03")]  # cursor - 3d .. today + 1
    assert _cursor(factory, CURSOR_ACTIVITIES) == "2026-10-02"
    # First sync without a cursor uses the initial window.
    with factory() as s:
        SyncCursorRepo(s).delete(SOURCE, CURSOR_ACTIVITIES)
        s.commit()
    syncer.run(("activities",))
    assert icu.activity_requests()[-1] == ("2026-09-02", "2026-10-03")


def test_no_streams_option_leaves_queue_pending(
    icu: FakeIcu,
    client: IntervalsClient,
    factory: sessionmaker[Session],
    stream_store: StreamStore,
) -> None:
    syncer = IntervalsSyncer(
        client,
        factory,
        stream_store,
        options=SyncOptions(fetch_streams=False, initial_days=30),
        clock=lambda: NOW_LOCAL,
    )
    report = syncer.run(("activities",))
    assert icu.r_streams.call_count == 0
    assert "streams_written" not in report.stages["activities"]
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None and ride.pending_streams is True
        assert [a.intervals_id for a in ActivityRepo(s).list_pending("streams")] == ["i3001"]
    # A later run with streams enabled picks the pending ride up even though it is unchanged.
    syncer.options.fetch_streams = True
    report = syncer.run(("activities",))
    assert report.stages["activities"]["streams_written"] == 1
    assert report.stages["activities"]["activities_unchanged"] == 2  # overlap window: 2 listed
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None and ride.pending_streams is False


def test_empty_stream_payload_closes_queue(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.streams["i3001"] = []
    report = syncer.run(("activities",))
    assert report.stages["activities"]["streams_empty"] == 1
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None and ride.pending_streams is False
        assert ride.stage_flags is not None
        assert ride.stage_flags["streams_skipped"] == "empty_payload"


def test_activity_failure_is_counted_and_does_not_abort_stage(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.r_streams.mock(return_value=httpx.Response(500))
    syncer.client.max_retries = 0
    report = syncer.run(("activities",))
    assert report.stages["activities"]["activities_failed"] == 1
    assert report.stages["activities"]["activities_new"] == 3
    with factory() as s:
        ride = ActivityRepo(s).get_by_intervals_id("i3001")
        assert ride is not None and ride.pending_streams is True  # retried next run
        run = JobRunRepo(s).latest("sync:icu:activities")
        assert run is not None and run.status == "ok"


# ---------------------------------------------------------------------------- wellness stage


def test_wellness_stage_and_fitness_mirror(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    report = syncer.run(("wellness",))
    assert report.stages["wellness"] == {"wellness_days": 10}
    assert _cursor(factory, CURSOR_WELLNESS) == TODAY.isoformat()
    p = icu.r_wellness.calls.last.request.url.params
    assert p["oldest"] == "2026-09-02" and p["newest"] == "2026-10-02"
    pk = _athlete_pk(factory)
    with factory() as s:
        repo = WellnessRepo(s)
        rows = repo.list_between(pk, dt.date(2026, 9, 23), dt.date(2026, 10, 2))
        assert len(rows) == 10
        day = repo.get(pk, dt.date(2026, 9, 28))
        src = next(w for w in icu.wellness if w["id"] == "2026-09-28")
        assert day is not None
        assert day.ctl == src["ctl"] and day.atl == src["atl"] and day.ramp_rate == src["rampRate"]
        assert day.resting_hr == src["restingHR"] and day.hrv == src["hrv"]
        assert day.sleep_s == src["sleepSecs"] and day.sleep_score == src["sleepScore"]
        assert day.readiness_icu == src["readiness"] and day.weight_kg == src["weight"]
        assert day.comments == "legs heavy" and day.raw_json == src
        fit = repo.fitness_between(pk, dt.date(2026, 9, 28), dt.date(2026, 9, 28))[0]
        assert fit.ctl_icu == src["ctl"] and fit.atl_icu == src["atl"]
        assert fit.tsb_icu == round(src["ctl"] - src["atl"], 2)
        assert fit.ctl_sim is None  # simulator columns untouched
        assert repo.latest_date(pk) == dt.date(2026, 10, 2)

    with factory() as s:
        SyncCursorRepo(s).set(SOURCE, CURSOR_WELLNESS, "2026-10-01")
        s.commit()
    icu.wellness[-1]["hrv"] = 99.0
    syncer.run(("wellness",))
    p = icu.r_wellness.calls.last.request.url.params
    assert p["oldest"] == "2026-09-24"  # cursor - 7 day overlap
    with factory() as s:
        day = WellnessRepo(s).get(pk, dt.date(2026, 10, 2))
        assert day is not None and day.hrv == 99.0


# ------------------------------------------------------------------------ power curves stage


def test_power_curves_stage_snapshots_three_windows(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    report = syncer.run(("power_curves",))
    assert report.stages["power_curves"] == {"power_curves": 3}
    p = icu.r_power_curves.calls.last.request.url.params
    assert p["type"] == "Ride" and p["curves"] == "42d,90d,s0" and p["newest"] == "2026-10-02"
    pk = _athlete_pk(factory)
    with factory() as s:
        repo = PowerCurveRepo(s)
        snaps = repo.list_for(pk)
        assert sorted(sn.window for sn in snaps) == ["42d", "90d", "season"]
        season = repo.latest(pk, "season")
        assert season is not None and season.as_of_date == TODAY and season.source == "icu"
        src = next(c for c in icu.power_curves["list"] if c["id"] == "s0")
        assert season.durations_s == src["secs"]
        assert season.watts == src["values"]
        assert season.w_kg is not None and season.w_kg[0] == round(src["values"][0] / 64.0, 3)
        assert (season.cp, season.w_prime, season.p_max, season.eftp_icu) == (262, 19500, 1040, 258)
        assert season.raw_json is not None and season.raw_json["mmp_model"]["type"] == "MS_2P"
    # Re-running the same day updates in place (unique key), no duplicates.
    syncer.run(("power_curves",))
    with factory() as s:
        assert len(PowerCurveRepo(s).list_for(pk)) == 3


def test_power_curves_without_mmp_model_still_snapshot(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.r_mmp.mock(return_value=httpx.Response(404))
    report = syncer.run(("power_curves",))
    assert report.stages["power_curves"]["power_curves"] == 3
    with factory() as s:
        snap = PowerCurveRepo(s).latest(_athlete_pk(factory), "42d")
        assert snap is not None and snap.cp is None and snap.watts is not None


# ---------------------------------------------------------------------------- events stage


def test_events_stage_mirrors_and_prunes(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    with factory() as s:  # a stale mirrored event icu no longer has
        IcuEventRepo(s).upsert(
            {"id": 77777, "category": "WORKOUT", "start_date_local": "2026-10-04T00:00:00"}
        )
        IcuEventRepo(s).upsert(
            {"id": 66666, "category": "SET_EFTP", "start_date_local": "2026-10-04T00:00:00"}
        )
        s.commit()
    report = syncer.run(("events",))
    assert report.stages["events"] == {
        "events": 3,
        "events_ours": 1,
        "fitness_model_events": 2,
        "events_pruned": 1,
    }
    p = icu.r_events.calls.last.request.url.params
    assert p["oldest"] == "2026-09-18" and p["newest"] == "2027-10-02"
    with factory() as s:
        repo = IcuEventRepo(s)
        assert repo.get(77777) is None  # pruned
        assert repo.get(66666) is not None  # fitness-model categories are never pruned
        ours = repo.list_between("2026-09-01", "2027-12-31", ours=True)
        assert [e.id for e in ours] == [90001]
        assert ours[0].external_id == "cyp:season-2026a:2026-10-06:1"
        assert ours[0].icu_training_load == 82 and ours[0].type == "Ride"
        note = repo.get(90003)
        assert note is not None
        assert note.category == "NOTE" and note.training_availability == "LIMITED"
        assert note.max_training_time == 3600 and note.end_date_local == "2026-10-09T00:00:00"
        theirs = repo.list_between("2026-09-01", "2027-12-31", ours=False, category="WORKOUT")
        assert [e.id for e in theirs] == [90004]
        eftp = repo.get(80001)
        assert eftp is not None and eftp.raw_json is not None and eftp.raw_json["icu_ftp"] == 250
        assert s.scalars(select(IcuEvent)).all().__len__() == 7


# ---------------------------------------------------------------------------- full run


def test_full_run_records_a_job_run_per_stage(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    report = syncer.run()
    assert list(report.stages) == ["athlete", "activities", "wellness", "power_curves", "events"]
    assert report.total("activities_new") == 3
    with factory() as s:
        jobs = sorted(j for j in s.scalars(select(JobRun.job)).all())
    assert jobs == [
        "sync:icu:activities",
        "sync:icu:athlete",
        "sync:icu:events",
        "sync:icu:power_curves",
        "sync:icu:wellness",
    ]


def test_unknown_stage_rejected(syncer: IntervalsSyncer) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        syncer.run(("nope",))


def test_stage_failure_recorded_and_raised(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.r_wellness.mock(return_value=httpx.Response(500))
    syncer.client.max_retries = 0
    syncer.run(("athlete",))
    with pytest.raises(IngestError):
        syncer.run(("wellness",))
    with factory() as s:
        run = JobRunRepo(s).latest("sync:icu:wellness")
        assert run is not None and run.status == "failed"
        assert run.error is not None and "IngestError" in run.error


# ---------------------------------------------------------------------------- backfill


def test_backfill_pages_by_month_and_sets_cursors(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.clone_ride("i2001", "2026-08-05T06:00:00")
    icu.clone_ride("i1001", "2026-07-27T06:00:00")
    report = syncer.backfill(70)
    pages = icu.activity_requests()
    assert pages == [
        ("2026-07-24", "2026-08-23"),
        ("2026-08-24", "2026-09-23"),
        ("2026-09-24", "2026-10-02"),
    ]
    assert [c.request.url.params["oldest"] for c in icu.r_wellness.calls] == [
        "2026-07-24",
        "2026-08-24",
        "2026-09-24",
    ]
    assert report.stages["backfill"]["pages"] == 3
    assert report.stages["backfill"]["activities_new"] == 5
    assert report.stages["backfill"]["streams_written"] == 3
    assert "power_curves" in report.stages and "events" in report.stages
    assert _cursor(factory, CURSOR_ACTIVITIES) == TODAY.isoformat()
    assert _cursor(factory, CURSOR_WELLNESS) == TODAY.isoformat()
    assert _cursor(factory, CURSOR_BACKFILL_PROGRESS) is None  # finished -> cleared
    with factory() as s:
        assert ActivityRepo(s).count() == 5
        run = JobRunRepo(s).latest("backfill:icu")
        assert run is not None and run.status == "ok"
        assert (
            run.rate_limit_snapshot is not None
            and run.rate_limit_snapshot["intervals_limit"] == 2500
        )


def test_backfill_resumes_from_persisted_page(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.clone_ride("i1001", "2026-07-27T06:00:00")
    # The second page blows up (icu down) after the first page committed.
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(503)
        return icu._list_activities(request)

    icu.r_activities.mock(side_effect=flaky)
    syncer.client.max_retries = 0
    with pytest.raises(IngestError):
        syncer.backfill(70)
    assert _cursor(factory, CURSOR_BACKFILL_PROGRESS) == "2026-07-24|2026-07-24"
    assert _cursor(factory, CURSOR_ACTIVITIES) is None  # never reached the present
    with factory() as s:
        assert ActivityRepo(s).count() == 1
        run = JobRunRepo(s).latest("backfill:icu")
        assert run is not None and run.status == "failed"

    icu.r_activities.mock(side_effect=icu._list_activities)
    report = syncer.backfill(70)
    resumed = icu.activity_requests()[2:]  # skip the two calls from the failed run
    assert resumed[0] == ("2026-07-24", "2026-08-23")  # page 1 start - 3d overlap, clamped
    assert resumed[-1][1] == "2026-10-02"
    assert len(resumed) == 3
    assert report.stages["backfill"]["pages"] == 3
    assert _cursor(factory, CURSOR_BACKFILL_PROGRESS) is None
    assert _cursor(factory, CURSOR_ACTIVITIES) == TODAY.isoformat()
    with factory() as s:
        assert ActivityRepo(s).count() == 4


def test_backfill_resume_skips_finished_pages_and_restarts_on_new_span(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    with factory() as s:  # pretend pages 1 and 2 of a 70-day backfill finished earlier
        SyncCursorRepo(s).set(SOURCE, CURSOR_BACKFILL_PROGRESS, "2026-07-24|2026-08-24")
        s.commit()
    syncer.backfill(70)
    assert icu.activity_requests()[0] == ("2026-08-21", "2026-09-20")  # resumes at page 2 - 3d
    # A different span (more days) starts over from its own oldest day.
    with factory() as s:
        SyncCursorRepo(s).set(SOURCE, CURSOR_BACKFILL_PROGRESS, "2026-07-24|2026-08-24")
        s.commit()
    before = len(icu.activity_requests())
    syncer.backfill(100)
    assert icu.activity_requests()[before] == ("2026-06-24", "2026-07-24")


def test_backfill_only_selected_stages(icu: FakeIcu, syncer: IntervalsSyncer) -> None:
    report = syncer.backfill(10, stages=("athlete", "wellness"))
    assert set(report.stages) == {"athlete", "backfill"}
    assert icu.r_activities.call_count == 0
    assert icu.r_wellness.call_count == 1
    assert icu.r_events.call_count == 0
    assert report.stages["backfill"]["wellness_days"] == 10
    assert ok([]).status_code == 200


def test_strava_origin_stub_attaches_to_existing_strava_row(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    """Post-Nov-2024 icu returns a bare stub for Strava-origin rides; its id is the Strava id."""
    icu.activities.append(
        {
            "id": "19094204373",
            "icu_athlete_id": ATHLETE_ID,
            "start_date_local": "2026-09-29T15:02:07",
            "source": "STRAVA",
            "_note": "STRAVA activities are not available via the API",
        }
    )
    with factory() as s:  # the Strava sync got there first
        ActivityRepo(s).upsert(
            {
                "strava_id": 19094204373,
                "match_method": "single_source",
                "sport_type": "VirtualRide",
                "name": "Rouvy climb",
                "start_utc": "2026-09-29T07:02:07Z",
                "moving_s": 1201,
                "raw_strava_json": {"id": 19094204373},
                "pending_detail": False,
                "stage_flags": {"detail": True, "efforts": True},
            }
        )
        s.commit()
    report = syncer.run(("activities",))
    counts = report.stages["activities"]
    assert counts["activities_joined_strava"] == 1 and counts["activities_new"] == 3
    assert "streams_skipped_strava" in counts  # i3002 only; the stub is not re-flagged
    with factory() as s:
        repo = ActivityRepo(s)
        assert repo.count() == 4
        row = repo.get_by_strava_id(19094204373)
        assert row is not None and row.intervals_id == "19094204373"
        assert row.match_method == "strava_id"
        # Strava's data survives; only the stub payload is attached
        assert row.sport_type == "VirtualRide" and row.is_ride is True and row.moving_s == 1201
        assert row.name == "Rouvy climb" and row.raw_intervals_json is not None
        assert row.raw_intervals_json["_note"].startswith("STRAVA activities")
        assert row.stage_flags == {"detail": True, "efforts": True}
        assert row.pending_detail is False
    # unchanged on rerun
    again = syncer.run(("activities",))
    assert again.stages["activities"]["activities_unchanged"] == 3  # i3002 left the window


def test_strava_origin_stub_alone_gets_strava_id(
    icu: FakeIcu, syncer: IntervalsSyncer, factory: sessionmaker[Session]
) -> None:
    icu.activities.append(
        {
            "id": "19094204373",
            "icu_athlete_id": ATHLETE_ID,
            "start_date_local": "2026-09-29T15:02:07",
            "source": "STRAVA",
        }
    )
    syncer.run(("activities",))
    with factory() as s:
        row = ActivityRepo(s).get_by_intervals_id("19094204373")
        assert row is not None and row.strava_id == 19094204373
        assert row.match_method == "strava_id" and row.sport_type == "Other"
        assert row.is_ride is False and row.pending_streams is False
        assert row.stage_flags is not None and row.stage_flags["streams_skipped"] == "strava_origin"
