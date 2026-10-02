"""job_run context manager and the repositories it relies on."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cyp.jobs.runs import job_run
from cyp.store.repo import ActivityRepo, JobRunRepo, SyncCursorRepo


def test_success_recorded(factory: sessionmaker[Session]) -> None:
    with job_run("sync", factory) as ctx:
        ctx.incr("activities", 3)
        ctx.incr("activities")
        ctx.rate_limit_snapshot = {"strava_15m_remaining": 95}
    with factory() as s:
        run = JobRunRepo(s).latest("sync")
    assert run is not None
    assert run.status == "ok"
    assert run.finished_at is not None
    assert run.counts == {"activities": 4}
    assert run.rate_limit_snapshot == {"strava_15m_remaining": 95}
    assert run.error is None


def test_failure_recorded_and_reraised(factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError, match="boom"), job_run("analyze", factory) as ctx:
        ctx.incr("rides", 2)
        raise RuntimeError("boom")
    with factory() as s:
        run = JobRunRepo(s).latest("analyze")
    assert run is not None
    assert run.status == "failed"
    assert run.counts == {"rides": 2}
    assert run.error is not None and "RuntimeError: boom" in run.error


def test_latest_per_job(factory: sessionmaker[Session]) -> None:
    with job_run("daily", factory):
        pass
    with job_run("daily", factory):
        pass
    with job_run("weekly", factory):
        pass
    with factory() as s:
        latest = JobRunRepo(s).latest_per_job()
        assert set(latest) == {"daily", "weekly"}
        assert len(JobRunRepo(s).list_for("daily")) == 2


def test_sync_cursor_repo(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        repo = SyncCursorRepo(s)
        assert repo.get("strava", "activities_after") is None
        repo.set("strava", "activities_after", "1700000000")
        repo.set("strava", "activities_after", "1700000100")
        s.commit()
    with factory() as s:
        repo = SyncCursorRepo(s)
        assert repo.get("strava", "activities_after") == "1700000100"
        assert len(repo.all_for("strava")) == 1
        assert repo.delete("strava", "activities_after") is True
        assert repo.get("strava", "activities_after") is None


def test_activity_repo_upsert_and_pending(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        repo = ActivityRepo(s)
        a = repo.upsert(
            {
                "strava_id": 1,
                "sport_type": "Ride",
                "start_utc": "2026-10-01T22:00:00Z",
                "moving_s": 3600,
                "elapsed_s": 3700,
                "name": "morning",
            }
        )
        b = repo.upsert({"strava_id": 1, "intervals_id": "i99", "name": "renamed"})
        assert a.id == b.id
        assert b.is_ride is True
        assert b.name == "renamed"
        repo.upsert(
            {
                "intervals_id": "i100",
                "sport_type": "Yoga",
                "start_utc": "2026-10-02T01:00:00Z",
            }
        )
        s.commit()
    with factory() as s:
        repo = ActivityRepo(s)
        assert repo.count() == 2
        assert repo.get_by_intervals_id("i99") is not None
        pending = repo.list_pending("analysis")
        assert [p.strava_id for p in pending] == [1, None]
        repo.mark_done(pending[0], "analysis")
        assert [p.intervals_id for p in repo.list_pending("analysis")] == ["i100"]
        assert pending[0].stage_flags == {"analyzed": True}
        rides = repo.list_between("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
        assert [r.strava_id for r in rides] == [1]
