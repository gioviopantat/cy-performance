"""Unified sync orchestration behind ``cyp sync`` and ``cyp backfill`` (docs/01 §5.7).

Order (ADR-0003: intervals.icu is primary, Strava secondary)::

    intervals (athlete, activities, wellness, power_curves, events | backfill)
      -> matcher.rematch_all            # label icu rows, merge any Strava rows synced earlier
      -> strava (if STRAVA_ENABLED)     # summaries, detail/efforts, zones, fallback streams
      -> matcher.rematch_all            # merge the Strava rows just created onto icu rows

Every stage runs under :func:`cyp.jobs.runs.job_run`; the whole run is additionally recorded
as one ``sync`` (or ``backfill``) row whose counts aggregate the stages. A failing source is
recorded and the others still run (docs/02 §4); the aggregated error is raised at the end so
the umbrella row is ``failed`` and the CLI exits non-zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import ConfigError, CypError, IngestError
from cyp.ingest.intervals.sync import STAGES, IntervalsSyncer, SyncOptions, SyncReport
from cyp.ingest.matcher import JOB as MATCH_JOB
from cyp.ingest.matcher import Matcher, MatchReport
from cyp.ingest.strava.sync import StravaSyncer, StravaSyncSummary
from cyp.jobs.runs import job_run
from cyp.logging import get_logger
from cyp.settings import Settings
from cyp.store.db import engine_from_settings, session_factory
from cyp.store.streams import StreamStore

log = get_logger(__name__)

JOB_SYNC = "sync"
JOB_BACKFILL = "backfill"
JOB_STRAVA = "sync:strava"


class IntervalsRunner(Protocol):
    """What :func:`run_sync` needs from an intervals.icu syncer."""

    def run(self, stages: tuple[str, ...] = STAGES) -> SyncReport:
        """Incremental sync of ``stages``."""
        ...

    def backfill(self, days: int, *, stages: tuple[str, ...] = STAGES) -> SyncReport:
        """Import ``days`` of history."""
        ...


class StravaRunner(Protocol):
    """What :func:`run_sync` needs from a Strava syncer."""

    def run(self, ctx: Any = None, *, full: bool = False) -> StravaSyncSummary:
        """One Strava sync (listing, details, fallback streams)."""
        ...


class MatchRunner(Protocol):
    """What :func:`run_sync` needs from the matcher."""

    def rematch_all(self, ctx: Any = None) -> MatchReport:
        """Merge duplicates and relabel ``match_method``."""
        ...


@dataclass
class SyncResult:
    """Outcome of one unified run (what the CLI prints)."""

    job: str
    intervals: dict[str, dict[str, int]] = field(default_factory=dict)
    match_before_strava: dict[str, int] = field(default_factory=dict)
    strava: dict[str, int] | None = None
    strava_skipped_reason: str | None = None
    strava_rate_limited: bool = False
    strava_remaining_15m: int | None = None
    match_after_strava: dict[str, int] | None = None
    errors: list[str] = field(default_factory=list)
    stages_run: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no source failed (a Strava rate limit is not a failure)."""
        return not self.errors


# ---------------------------------------------------------------------------------- builders


class _ClosingIntervalsSyncer(IntervalsSyncer):
    """IntervalsSyncer that also owns (and closes) its HTTP client."""

    def close(self) -> None:
        self.client.close()


def build_intervals_syncer(
    settings: Settings, factory: sessionmaker[Session], *, streams: bool
) -> IntervalsRunner:
    """Real intervals.icu syncer from settings.

    Raises:
        ConfigError: ``INTERVALS_API_KEY`` is not set.
    """
    from cyp.ingest.intervals.auth import ApiKeyAuth
    from cyp.ingest.intervals.client import IntervalsClient

    key = settings.intervals_api_key.get_secret_value()
    if not key:
        raise ConfigError("INTERVALS_API_KEY is not set (see .env.example)")
    client = IntervalsClient(auth=ApiKeyAuth(key), athlete_id=settings.intervals_athlete_id)
    return _ClosingIntervalsSyncer(
        client,
        factory,
        StreamStore(settings.streams_dir),
        options=SyncOptions(fetch_streams=streams),
        timezone=settings.cyp_timezone,
        log_path=str(settings.logs_dir / "cyp.jsonl"),
    )


def build_strava_syncer(
    settings: Settings, factory: sessionmaker[Session], *, streams: bool, no_wait: bool
) -> StravaRunner:
    """Real Strava syncer from settings (token must exist; see ``cyp auth strava``)."""
    from cyp.ingest.strava.client import StravaClient
    from cyp.ingest.strava.oauth import StravaAuth, TokenStore

    auth = StravaAuth(settings, TokenStore.from_settings(settings))
    client = StravaClient(
        auth.get_valid_access_token,
        auth.force_refresh_access_token,
        wait_on_rate_limit=not no_wait,
    )
    return StravaSyncer(
        settings,
        factory,
        client,
        StreamStore(settings.streams_dir),
        max_detail_fetches=settings.strava_max_detail_fetches,
        fetch_streams=streams,
    )


def build_matcher(settings: Settings, factory: sessionmaker[Session]) -> MatchRunner:
    """Real matcher over the configured stream store."""
    return Matcher(factory, StreamStore(settings.streams_dir))


IntervalsFactory = Callable[[Settings, sessionmaker[Session]], IntervalsRunner]
StravaFactory = Callable[[Settings, sessionmaker[Session]], StravaRunner]
MatcherFactory = Callable[[Settings, sessionmaker[Session]], MatchRunner]


# ------------------------------------------------------------------------------------- runner


def run_sync(
    settings: Settings,
    *,
    backfill_days: int | None = None,
    streams: bool = True,
    no_wait: bool = False,
    factory: sessionmaker[Session] | None = None,
    intervals: IntervalsFactory | None = None,
    strava: StravaFactory | None = None,
    matcher: MatcherFactory | None = None,
) -> SyncResult:
    """Run intervals -> matcher -> strava -> matcher; see the module docstring.

    ``backfill_days`` switches the intervals part to :meth:`IntervalsSyncer.backfill` and
    records the umbrella row as ``backfill``. The ``intervals`` / ``strava`` / ``matcher``
    factories exist for tests (fakes); production uses the ``build_*`` functions.

    Raises:
        IngestError: a source failed (after every other stage ran); recorded on the umbrella
            ``job_runs`` row.
    """
    job = JOB_BACKFILL if backfill_days is not None else JOB_SYNC
    result = SyncResult(job=job)
    engine = None
    if factory is None:
        engine = engine_from_settings(settings)
        factory = session_factory(engine)
    make_intervals = intervals or (lambda s, f: build_intervals_syncer(s, f, streams=streams))
    make_strava = strava or (
        lambda s, f: build_strava_syncer(s, f, streams=streams, no_wait=no_wait)
    )
    make_matcher = matcher or build_matcher
    log_path = str(settings.logs_dir / "cyp.jsonl")
    try:
        with job_run(job, factory, log_path=log_path) as ctx:
            _run_intervals(settings, factory, make_intervals, backfill_days, result, ctx)
            result.match_before_strava = _run_matcher(settings, factory, make_matcher, result)
            _run_strava(settings, factory, make_strava, result, ctx)
            if result.strava is not None:
                result.match_after_strava = _run_matcher(settings, factory, make_matcher, result)
            ctx.incr("errors", len(result.errors))
            if result.errors:
                raise IngestError("; ".join(result.errors))
    finally:
        if engine is not None:
            engine.dispose()
    return result


def _run_intervals(
    settings: Settings,
    factory: sessionmaker[Session],
    make: IntervalsFactory,
    backfill_days: int | None,
    result: SyncResult,
    ctx: Any,
) -> None:
    try:
        syncer = make(settings, factory)
    except CypError as exc:
        result.errors.append(f"intervals: {exc}")
        log.error("sync.intervals.unavailable", error=str(exc))
        return
    try:
        report = syncer.backfill(backfill_days) if backfill_days is not None else syncer.run()
    except CypError as exc:
        result.errors.append(f"intervals: {exc}")
        log.error("sync.intervals.failed", error=str(exc))
        return
    finally:
        close = getattr(syncer, "close", None)
        if callable(close):
            close()
    result.intervals = dict(report.stages)
    result.stages_run.extend(f"intervals:{name}" for name in report.stages)
    for key in ("activities_new", "activities_updated", "streams_written", "wellness_days"):
        total = report.total(key)
        if total:
            ctx.incr(f"icu_{key}", total)


def _run_matcher(
    settings: Settings,
    factory: sessionmaker[Session],
    make: MatcherFactory,
    result: SyncResult,
) -> dict[str, int]:
    with job_run(MATCH_JOB, factory, log_path=str(settings.logs_dir / "cyp.jsonl")) as ctx:
        report = make(settings, factory).rematch_all(ctx)
    result.stages_run.append("match")
    return report.counts()


def _run_strava(
    settings: Settings,
    factory: sessionmaker[Session],
    make: StravaFactory,
    result: SyncResult,
    ctx: Any,
) -> None:
    if not settings.strava_enabled:
        result.strava_skipped_reason = "STRAVA_ENABLED=false"
        return
    try:
        syncer = make(settings, factory)
    except CypError as exc:
        result.errors.append(f"strava: {exc}")
        log.error("sync.strava.unavailable", error=str(exc))
        return
    with job_run(JOB_STRAVA, factory, log_path=str(settings.logs_dir / "cyp.jsonl")) as sctx:
        summary = syncer.run(sctx)
    result.stages_run.append("strava")
    if summary.skipped:
        result.strava_skipped_reason = "STRAVA_ENABLED=false"
        return
    result.strava = summary.counts()
    result.strava_rate_limited = summary.rate_limited
    remaining = summary.rate_limit.get("remaining_15m")
    result.strava_remaining_15m = int(remaining) if remaining is not None else None
    for key in ("activities_new", "details_fetched", "streams_fetched", "streams_fallback_fetched"):
        if result.strava.get(key):
            ctx.incr(f"strava_{key}", result.strava[key])
    # Strava's own per-activity errors are reported, not fatal: the source as a whole ran.
    for err in summary.errors:
        if not err.startswith("rate_limit:"):
            log.warning("sync.strava.activity_error", error=err)
