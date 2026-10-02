"""run_sync ordering (intervals -> match -> strava -> match) with fakes; job_runs bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import ConfigError, IngestError
from cyp.ingest.intervals.sync import STAGES, SyncReport
from cyp.ingest.matcher import MatchReport
from cyp.ingest.strava.sync import StravaSyncSummary
from cyp.jobs.sync import JOB_BACKFILL, JOB_STRAVA, JOB_SYNC, run_sync
from cyp.settings import Settings
from cyp.store.repo import JobRunRepo


@dataclass
class Calls:
    order: list[str] = field(default_factory=list)


class FakeIntervals:
    def __init__(self, calls: Calls, *, fail: bool = False) -> None:
        self.calls = calls
        self.fail = fail
        self.closed = False
        self.backfill_days: int | None = None

    def run(self, stages: tuple[str, ...] = STAGES) -> SyncReport:
        self.calls.order.append("intervals.run")
        if self.fail:
            raise IngestError("icu down")
        return SyncReport(stages={"activities": {"activities_new": 2, "streams_written": 1}})

    def backfill(self, days: int, *, stages: tuple[str, ...] = STAGES) -> SyncReport:
        self.calls.order.append("intervals.backfill")
        self.backfill_days = days
        return SyncReport(stages={"backfill": {"activities_new": 40, "pages": 2}})

    def close(self) -> None:
        self.closed = True


class FakeStrava:
    def __init__(self, calls: Calls, *, rate_limited: bool = False) -> None:
        self.calls = calls
        self.rate_limited = rate_limited
        self.ctx: Any = None

    def run(self, ctx: Any = None, *, full: bool = False) -> StravaSyncSummary:
        self.calls.order.append("strava.run")
        self.ctx = ctx
        summary = StravaSyncSummary(activities_new=3, details_fetched=3, streams_fallback_fetched=1)
        summary.rate_limit = {"remaining_15m": 80}
        if self.rate_limited:
            summary.rate_limited = True
            summary.errors.append("rate_limit: window exhausted")
        return summary


class FakeMatcher:
    def __init__(self, calls: Calls) -> None:
        self.calls = calls

    def rematch_all(self, ctx: Any = None) -> MatchReport:
        self.calls.order.append("match")
        if ctx is not None:
            ctx.incr("merged_by_strava_id", 1)
        return MatchReport(merged_by_strava_id=1, rows_by_method={"strava_id": 5})


def _jobs(factory: sessionmaker[Session]) -> dict[str, str]:
    with factory() as s:
        return {job: run.status for job, run in JobRunRepo(s).latest_per_job().items()}


def test_order_and_job_runs_with_strava(settings: Settings, factory: sessionmaker[Session]) -> None:
    calls = Calls()
    icu = FakeIntervals(calls)
    strava = FakeStrava(calls)
    result = run_sync(
        settings,
        factory=factory,
        intervals=lambda _s, _f: icu,
        strava=lambda _s, _f: strava,
        matcher=lambda _s, _f: FakeMatcher(calls),
    )
    assert calls.order == ["intervals.run", "match", "strava.run", "match"]
    assert result.ok and result.job == JOB_SYNC
    assert result.intervals == {"activities": {"activities_new": 2, "streams_written": 1}}
    assert result.match_before_strava["merged_by_strava_id"] == 1
    assert result.strava is not None and result.strava["streams_fallback_fetched"] == 1
    assert result.strava_remaining_15m == 80 and result.strava_rate_limited is False
    assert result.match_after_strava is not None
    assert result.stages_run == ["intervals:activities", "match", "strava", "match"]
    assert icu.closed is True
    assert strava.ctx is not None and strava.ctx.job == JOB_STRAVA
    jobs = _jobs(factory)
    assert jobs[JOB_SYNC] == "ok" and jobs["match"] == "ok" and jobs[JOB_STRAVA] == "ok"
    with factory() as s:
        umbrella = JobRunRepo(s).latest(JOB_SYNC)
        assert umbrella is not None
        assert umbrella.counts["icu_activities_new"] == 2
        assert umbrella.counts["strava_details_fetched"] == 3 and umbrella.counts["errors"] == 0
        assert len(JobRunRepo(s).list_for("match")) == 2


def test_strava_disabled_runs_matcher_once(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    calls = Calls()
    disabled = settings.model_copy(update={"strava_enabled": False})

    def never(_s: Settings, _f: sessionmaker[Session]) -> FakeStrava:
        raise AssertionError("strava must not be built when disabled")

    result = run_sync(
        disabled,
        factory=factory,
        intervals=lambda _s, _f: FakeIntervals(calls),
        strava=never,
        matcher=lambda _s, _f: FakeMatcher(calls),
    )
    assert calls.order == ["intervals.run", "match"]
    assert result.strava is None and result.strava_skipped_reason == "STRAVA_ENABLED=false"
    assert result.match_after_strava is None and result.ok
    assert JOB_STRAVA not in _jobs(factory)


def test_backfill_days_switches_to_backfill(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    calls = Calls()
    icu = FakeIntervals(calls)
    result = run_sync(
        settings.model_copy(update={"strava_enabled": False}),
        factory=factory,
        backfill_days=365,
        intervals=lambda _s, _f: icu,
        matcher=lambda _s, _f: FakeMatcher(calls),
    )
    assert calls.order == ["intervals.backfill", "match"] and icu.backfill_days == 365
    assert result.job == JOB_BACKFILL and result.intervals["backfill"]["pages"] == 2
    jobs = _jobs(factory)
    assert jobs[JOB_BACKFILL] == "ok" and JOB_SYNC not in jobs


def test_intervals_failure_still_runs_strava_then_raises(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    calls = Calls()
    with pytest.raises(IngestError, match="icu down"):
        run_sync(
            settings,
            factory=factory,
            intervals=lambda _s, _f: FakeIntervals(calls, fail=True),
            strava=lambda _s, _f: FakeStrava(calls),
            matcher=lambda _s, _f: FakeMatcher(calls),
        )
    assert calls.order == ["intervals.run", "match", "strava.run", "match"]
    jobs = _jobs(factory)
    assert jobs[JOB_SYNC] == "failed" and jobs[JOB_STRAVA] == "ok"
    with factory() as s:
        run = JobRunRepo(s).latest(JOB_SYNC)
        assert run is not None and run.error is not None and "icu down" in run.error
        assert run.counts["errors"] == 1


def test_missing_intervals_key_is_reported_not_fatal_for_strava(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    calls = Calls()

    def no_key(_s: Settings, _f: sessionmaker[Session]) -> FakeIntervals:
        raise ConfigError("INTERVALS_API_KEY is not set")

    with pytest.raises(IngestError, match="INTERVALS_API_KEY"):
        run_sync(
            settings,
            factory=factory,
            intervals=no_key,
            strava=lambda _s, _f: FakeStrava(calls),
            matcher=lambda _s, _f: FakeMatcher(calls),
        )
    assert calls.order == ["match", "strava.run", "match"]


def test_strava_rate_limit_is_flagged_not_an_error(
    settings: Settings, factory: sessionmaker[Session]
) -> None:
    calls = Calls()
    result = run_sync(
        settings,
        factory=factory,
        intervals=lambda _s, _f: FakeIntervals(calls),
        strava=lambda _s, _f: FakeStrava(calls, rate_limited=True),
        matcher=lambda _s, _f: FakeMatcher(calls),
    )
    assert result.ok and result.strava_rate_limited is True
    assert _jobs(factory)[JOB_SYNC] == "ok"
