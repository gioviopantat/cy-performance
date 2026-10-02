"""Matcher: strava_id merge (incl. icu stubs), time-window merge, relabelling, idempotency."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.ingest.matcher import Matcher, effective_strava_id, is_icu_stub, within_time_window
from cyp.jobs.runs import job_run
from cyp.store.models import Activity, ActivityZones, SegmentEffort, StreamFile
from cyp.store.repo import ActivityRepo, JobRunRepo
from cyp.store.repo.segments import SegmentRepo
from cyp.store.repo.zones import ActivityZonesRepo
from cyp.store.streams import StreamStore

STUB_NOTE = "STRAVA activities are not available via the API"


def _icu(intervals_id: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "intervals_id": intervals_id,
        "sport_type": "Ride",
        "name": "icu ride",
        "start_utc": "2026-05-22T12:59:15Z",
        "moving_s": 2372,
        "elapsed_s": 3470,
        "avg_w": 201.0,
        "icu_training_load": 55.0,
        "raw_intervals_json": {"id": intervals_id, "type": "Ride", "source": "GARMIN_CONNECT"},
        "pending_detail": False,
        "pending_streams": False,
        "stage_flags": {"detail": True, "streams": True},
    }
    row.update(extra)
    return row


def _stub(strava_id: int, start_local: str = "2026-06-28T15:02:07") -> dict[str, Any]:
    return {
        "intervals_id": str(strava_id),
        "sport_type": "Other",
        "is_ride": False,
        "start_utc": "2026-06-28T07:02:07Z",
        "raw_intervals_json": {
            "id": str(strava_id),
            "source": "STRAVA",
            "start_date_local": start_local,
            "_note": STUB_NOTE,
        },
        "pending_detail": False,
        "pending_streams": False,
        "stage_flags": {"detail": True, "streams": False, "streams_skipped": "strava_origin"},
    }


def _strava(strava_id: int, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strava_id": strava_id,
        "match_method": "single_source",
        "sport_type": "VirtualRide",
        "name": "strava ride",
        "description": "Rouvy climb",
        "start_utc": "2026-06-28T07:02:07Z",
        "moving_s": 1201,
        "elapsed_s": 1210,
        "device_name": "Rouvy",
        "avg_w": 190.0,
        "raw_strava_json": {"id": strava_id, "laps": [{"id": 1}]},
        "pending_detail": False,
        "pending_streams": False,
        "stage_flags": {"detail": True, "efforts": True, "zones": True, "streams": True},
    }
    row.update(extra)
    return row


def _insert(factory: sessionmaker[Session], *rows: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    with factory() as s:
        repo = ActivityRepo(s)
        for row in rows:
            ids.append(repo.upsert(row).id)
        s.commit()
    return ids


def _write_stream(
    factory: sessionmaker[Session], store: StreamStore, activity_id: int, source: str
) -> Path:
    path = store.write(activity_id, pl.DataFrame({"t_s": [0, 1, 2], "watts": [100, 110, 120]}))
    raw = path.with_suffix(f".{source}.json") if source == "strava" else None
    if raw is not None:
        raw.write_text("{}")
    with factory() as s:
        ActivityRepo(s).upsert_stream_file(
            activity_id,
            {
                "source": source,
                "path": str(path),
                "columns": ["t_s", "watts"],
                "hz": 1.0,
                "n_samples": 3,
                "raw_json_path": str(raw) if raw else None,
            },
        )
        s.commit()
    return path


def _matcher(factory: sessionmaker[Session], data_dir: Path) -> tuple[Matcher, StreamStore]:
    store = StreamStore(data_dir / "streams")
    return Matcher(factory, store), store


# ------------------------------------------------------------------------------ pure helpers


def test_stub_detection_and_effective_strava_id() -> None:
    stub = Activity(
        intervals_id="19094204373", raw_intervals_json=_stub(19094204373)["raw_intervals_json"]
    )
    assert is_icu_stub(stub) and effective_strava_id(stub) == 19094204373
    garmin = Activity(
        intervals_id="i1", raw_intervals_json={"type": "Ride", "source": "GARMIN_CONNECT"}
    )
    assert not is_icu_stub(garmin) and effective_strava_id(garmin) is None
    garmin.strava_id = 42
    assert effective_strava_id(garmin) == 42
    assert effective_strava_id(Activity(strava_id=7, intervals_id=None)) == 7


def test_within_time_window_uses_moving_or_elapsed() -> None:
    a = Activity(start_utc="2026-05-22T12:59:15Z", moving_s=2372, elapsed_s=3470)
    b = Activity(start_utc="2026-05-22T12:59:15Z", moving_s=2484, elapsed_s=3470)  # icu vs Strava
    assert within_time_window(a, b)  # moving differs 4.7 % but elapsed is identical
    c = Activity(start_utc="2026-05-22T13:01:30Z", moving_s=2372, elapsed_s=3470)
    assert not within_time_window(a, c)  # 135 s apart
    d = Activity(start_utc="2026-05-22T12:59:15Z", moving_s=2600, elapsed_s=3700)
    assert not within_time_window(a, d)  # both durations off by > 2 %
    assert not within_time_window(a, Activity(start_utc=None, moving_s=1, elapsed_s=1))


# ---------------------------------------------------------------------- rule 1: strava_id


def test_stub_and_strava_twin_merge_onto_icu_row(
    factory: sessionmaker[Session], data_dir: Path
) -> None:
    matcher, store = _matcher(factory, data_dir)
    stub_id, strava_pk = _insert(factory, _stub(19094204373), _strava(19094204373))
    parquet = _write_stream(factory, store, strava_pk, "strava")
    with factory() as s:
        SegmentRepo(s).upsert_segment({"id": 5001, "name": "Hill"})
        SegmentRepo(s).upsert_effort({"id": 9001, "activity_id": strava_pk, "segment_id": 5001})
        ActivityZonesRepo(s).upsert(strava_pk, "power", {"points": 3})
        s.commit()

    with job_run("match", factory) as ctx:
        report = matcher.rematch_all(ctx)

    assert report.merged_by_strava_id == 1 and report.streams_moved == 1
    assert report.rows_by_method == {"strava_id": 1}
    with factory() as s:
        assert ActivityRepo(s).count() == 1
        row = ActivityRepo(s).get(stub_id)
        assert row is not None and row.strava_id == 19094204373
        assert row.intervals_id == "19094204373"
        assert row.match_method == "strava_id"
        # the stub carried nothing: Strava's summary wins outright
        assert row.sport_type == "VirtualRide" and row.is_ride is True
        assert row.name == "strava ride" and row.moving_s == 1201 and row.device_name == "Rouvy"
        assert row.raw_strava_json is not None and row.raw_strava_json["laps"] == [{"id": 1}]
        assert row.raw_intervals_json is not None and row.raw_intervals_json["_note"] == STUB_NOTE
        assert row.pending_analysis is True and row.pending_detail is False
        assert row.stage_flags == {"detail": True, "efforts": True, "zones": True, "streams": True}
        sf = s.get(StreamFile, stub_id)
        assert sf is not None and sf.source == "strava" and sf.path == str(store.path_for(stub_id))
        assert sf.raw_json_path is not None and Path(sf.raw_json_path).is_file()
        assert s.get(StreamFile, strava_pk) is None
        efforts = s.scalars(select(SegmentEffort)).all()
        assert [e.activity_id for e in efforts] == [stub_id]
        zones = s.scalars(select(ActivityZones)).all()
        assert [z.activity_id for z in zones] == [stub_id]
        run = JobRunRepo(s).latest("match")
        assert run is not None and run.counts["merged_by_strava_id"] == 1
    assert store.exists(stub_id) and not parquet.exists()

    # idempotent: a second pass changes nothing
    again = matcher.rematch_all()
    assert again.merged_by_strava_id == 0 and again.relabelled == 0 and again.streams_moved == 0
    assert again.rows_by_method == {"strava_id": 1}


def test_icu_row_wins_and_duplicate_strava_streams_are_dropped(
    factory: sessionmaker[Session], data_dir: Path
) -> None:
    """A Garmin-direct icu ride and its Strava upload: icu values win, Strava fills gaps."""
    matcher, store = _matcher(factory, data_dir)
    icu_pk, strava_pk = _insert(
        factory,
        _icu("i1"),
        _strava(777, sport_type="Ride", start_utc="2026-05-22T12:59:15Z", elapsed_s=3470),
    )
    _write_stream(factory, store, icu_pk, "intervals")
    dup_parquet = _write_stream(factory, store, strava_pk, "strava")

    report = matcher.rematch_all()
    assert report.merged_by_time_window == 1 and report.streams_dropped == 1
    with factory() as s:
        row = ActivityRepo(s).get(icu_pk)
        assert row is not None and ActivityRepo(s).count() == 1
        assert row.match_method == "time_window" and row.strava_id == 777
        assert row.name == "icu ride" and row.moving_s == 2372 and row.sport_type == "Ride"
        assert row.description == "Rouvy climb" and row.device_name == "Rouvy"  # icu lacked them
        assert row.icu_training_load == 55.0 and row.raw_strava_json is not None
        sf = s.get(StreamFile, icu_pk)
        assert sf is not None and sf.source == "intervals"  # never mixed (ADR-0003)
        assert s.get(StreamFile, strava_pk) is None
    assert store.exists(icu_pk) and not dup_parquet.exists()


def test_stub_without_twin_gets_its_strava_id(
    factory: sessionmaker[Session], data_dir: Path
) -> None:
    matcher, _ = _matcher(factory, data_dir)
    (pk,) = _insert(factory, _stub(123))
    report = matcher.rematch_all()
    assert report.merged_by_strava_id == 0 and report.relabelled == 1
    with factory() as s:
        row = ActivityRepo(s).get(pk)
        assert row is not None and row.strava_id == 123 and row.match_method == "strava_id"
        assert row.sport_type == "Other"  # nothing to fill it from yet


# -------------------------------------------------------------------- rule 2: time window


def test_time_window_merge_and_non_matches_stay_single_source(
    factory: sessionmaker[Session], data_dir: Path
) -> None:
    matcher, store = _matcher(factory, data_dir)
    icu_pk, twin_pk, far_pk, other_icu = _insert(
        factory,
        _icu("i1"),
        _strava(
            4242,
            sport_type="Ride",
            start_utc="2026-05-22T12:59:15Z",
            moving_s=2484,
            elapsed_s=3470,
            description=None,
        ),
        _strava(
            4343, sport_type="Ride", start_utc="2026-05-22T13:04:00Z", moving_s=2372, elapsed_s=3470
        ),
        _icu("i2", start_utc="2026-05-23T10:00:00Z", moving_s=100, elapsed_s=100),
    )
    _write_stream(factory, store, twin_pk, "strava")

    report = matcher.rematch_all()
    assert report.merged_by_time_window == 1 and report.merged_by_strava_id == 0
    assert report.streams_moved == 1  # icu had no streams -> Strava's Parquet is used
    assert report.rows_by_method == {"single_source": 2, "time_window": 1}
    with factory() as s:
        row = ActivityRepo(s).get(icu_pk)
        assert row is not None and row.match_method == "time_window" and row.strava_id == 4242
        assert row.moving_s == 2372  # icu value kept
        assert ActivityRepo(s).get(twin_pk) is None
        far = ActivityRepo(s).get(far_pk)
        assert far is not None and far.match_method == "single_source"
        other = ActivityRepo(s).get(other_icu)
        assert other is not None and other.match_method == "single_source"
        assert s.get(StreamFile, icu_pk) is not None
    # idempotent and labels survive a second pass
    again = matcher.rematch_all()
    assert again.merged_by_time_window == 0 and again.relabelled == 0
    assert again.rows_by_method == {"single_source": 2, "time_window": 1}


def test_relabel_fixes_rows_with_both_ids(factory: sessionmaker[Session], data_dir: Path) -> None:
    matcher, _ = _matcher(factory, data_dir)
    both, lone = _insert(
        factory,
        _icu("i9", strava_id=99, match_method="single_source"),
        _strava(100, match_method="time_window"),  # bogus label on a single-source row
    )
    report = matcher.rematch_all()
    assert report.relabelled == 2 and report.rows_by_method == {"single_source": 1, "strava_id": 1}
    with factory() as s:
        a = ActivityRepo(s).get(both)
        b = ActivityRepo(s).get(lone)
        assert a is not None and a.match_method == "strava_id"
        assert b is not None and b.match_method == "single_source"
