"""Ride watcher: email each new ride's PDF report within minutes (docs/specs/ride-report.md).

A launch agent runs ``cyp watch --all`` every 10 minutes (no webhook: we have no public URL).
One tick for one profile::

    flags (report.ride) -> non-blocking profile lock (held -> skip, job_runs row)
      -> light intervals.icu sync (activities only; the key-owner check runs first)
      -> per-ride analysis -> for each ride of the last 2 days without a finished report:
         wait for streams (<= 6 h) and for the rating (<= 2 h), then build + email + row

Rows live in ``ride_reports`` (one per ride, so a ride is emailed once). A failed build or
email is retried on the next ticks up to 3 attempts, then a macOS notice. The watcher never
writes a calendar; tomorrow's plan in the PDF is a dry-run preview.

With ``strava.auto_description`` on (ADR-0020), a ride whose report was just done gets its
RIDE.LOG written to Strava through ``services/strava_write.auto_push`` (every gate of ``push``),
after ``services/ride_story.prepare`` looked the route up and narrated it (their own flags).
The outcome goes to ``ride_reports.strava_status``; a failure never touches the report row,
is retried once on the next tick, then given up (macOS notice). A story whose route lookup
failed (Overpass down) is prepared again hourly for a day and Strava rewritten when its text
changed.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, select

from cyp.core.errors import ConfigError, CypError
from cyp.core.timeutil import now_utc, to_local
from cyp.jobs.runner import ChildResult, RunLockedError, notify_failure, profile_lock, run_profiles
from cyp.logging import get_logger
from cyp.services import pipeline, ride_feedback, ride_report, ride_story, strava_write
from cyp.services import trends as trends_service
from cyp.services.context import AppContext
from cyp.settings import Settings
from cyp.store.migrate import upgrade_head
from cyp.store.models import Activity, RideReport
from cyp.store.runs import job_run

log = get_logger(__name__)

JOB = "watch"
#: Rides starting on this many local days (today included) are candidates.
WINDOW_DAYS = 2
#: Unrated rides are sent this long after the watcher first saw them.
FEEL_WAIT = dt.timedelta(hours=2)
#: Rides whose streams never arrive are sent without them after this long.
STREAMS_WAIT = dt.timedelta(hours=6)
#: A tick must end well before the next one (launchd skips a start while one still runs).
CHILD_TIMEOUT_S = 9 * 60

Decision = Literal["wait_streams", "wait_feel", "send"]
TickStatus = Literal["ok", "skipped", "failed"]
SyncFn = Callable[[AppContext], dict[str, Any]]


#: A ride's story whose route lookup failed is retried this often, for this long.
STORY_RETRY_EVERY = dt.timedelta(hours=1)
STORY_RETRY_FOR = dt.timedelta(days=1)


@dataclass
class RideTick:
    """What one tick did with one ride."""

    activity_id: int
    #: wait_streams | wait_feel | built | sent | failed | gave_up, or strava_<status> for the
    #: automatic Strava write (written | unchanged | failed | gave_up).
    action: str
    detail: str = ""


@dataclass
class WatchReport:
    """Outcome of one profile's tick (``cyp watch --json`` prints it)."""

    profile: str
    status: TickStatus
    detail: str = ""
    rides: list[RideTick] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """The tick ran (or was skipped) and no report failed."""
        return self.status != "failed" and all(
            r.action not in ("failed", "gave_up") for r in self.rides
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "profile": self.profile,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "rides": [asdict(r) for r in self.rides],
        }


class _TickFailedError(CypError):
    """Marks the ``job_runs`` row failed (the sync failed); the rides were still handled."""


def decide(
    first_seen: dt.datetime, now: dt.datetime, *, has_streams: bool, rated: bool
) -> Decision:
    """Send timing (spec, open question 2): streams first (<= 6 h), then the rating (<= 2 h)."""
    age = now - first_seen
    if not has_streams and age < STREAMS_WAIT:
        return "wait_streams"
    if not rated and age < FEEL_WAIT:
        return "wait_feel"
    return "send"


def watch_profile(
    settings: Settings,
    *,
    now: dt.datetime | None = None,
    mailer: ride_report.Mailer | None = None,
    sync: SyncFn | None = None,
    strava_client: strava_write.ClientFactory | None = None,
) -> WatchReport:
    """One tick for one profile; always returns a report (see the module docstring).

    With ``report.ride`` off it returns ``skipped`` without taking the lock or writing a row, so
    profiles that do not use the watcher get no row every 10 minutes.
    """
    profile = settings.cyp_profile or "default"
    ctx = AppContext.from_settings(settings)
    try:
        try:
            on = ctx.features().enabled("report.ride")
        except ConfigError as exc:
            return WatchReport(profile, "failed", f"config: {str(exc).splitlines()[0]}")
        if not on:
            return WatchReport(profile, "skipped", "feature report.ride is off")
        for path in settings.data_layout:
            path.mkdir(parents=True, exist_ok=True)
        with profile_lock(settings.cyp_data_dir):
            upgrade_head(settings.cyp_db_url)
            return tick(ctx, now=now, mailer=mailer, sync=sync, strava_client=strava_client)
    except RunLockedError:
        report = WatchReport(profile, "skipped", "another run holds the profile lock")
        _record_skip(ctx)
        return report
    except Exception as exc:  # the scheduler must always get a report
        log.exception("watch.setup_failed", profile=profile)
        return WatchReport(profile, "failed", f"setup: {type(exc).__name__}: {exc}")
    finally:
        ctx.close()


def tick(
    ctx: AppContext,
    *,
    now: dt.datetime | None = None,
    mailer: ride_report.Mailer | None = None,
    sync: SyncFn | None = None,
    strava_client: strava_write.ClientFactory | None = None,
) -> WatchReport:
    """The body of a tick, under the caller's lock. One ``job_runs`` row (``watch``).

    ``strava_client`` replaces the Strava client of the automatic write (tests).
    """
    at = now or now_utc()
    features = ctx.features()
    report = WatchReport(ctx.settings.cyp_profile or "default", "ok")
    log_path = str(ctx.settings.logs_dir / "cyp.jsonl")
    try:
        with job_run(JOB, ctx.factory, log_path=log_path) as run:
            sync_error = _sync(ctx, sync or _light_sync, features.enabled("sync.intervals"))
            pipeline.analyze(ctx)
            todo = _due(ctx, at, report)
            if todo:
                trends_service.recompute(ctx, as_of=to_local(at, ctx.settings.cyp_timezone).date())
            reported: list[int] = []
            for activity_id, detail in todo:
                out = ride_report.send(ctx, activity_id, mailer=mailer, now=at)
                report.rides.append(RideTick(activity_id, out.status, out.error or detail))
                if out.ok:
                    reported.append(activity_id)
                if out.status == ride_report.GAVE_UP:
                    notify_failure(ctx.settings, f"ride report {activity_id} not sent: {out.error}")
            if features.enabled(strava_write.AUTO_FLAG):
                retries = [i for i in ride_report.strava_retries(ctx) if i not in reported]
                # A story whose OSM lookup failed: looked up again hourly for a day, and
                # Strava rewritten when the text changed (push is a no-op otherwise).
                stories = [
                    i
                    for i in ride_report.strava_done_since(ctx, at - STORY_RETRY_FOR)
                    if i not in reported
                    and i not in retries
                    and ride_story.needs_retry(ctx, i, at, STORY_RETRY_EVERY)
                ]
                for activity_id in [*reported, *retries, *stories]:
                    report.rides.append(_strava(ctx, activity_id, at, strava_client))
            for r in report.rides:
                run.incr(r.action)
            if sync_error:
                raise _TickFailedError(sync_error)
    except _TickFailedError as exc:
        report.status, report.detail = "failed", str(exc)
    return report


def _strava(
    ctx: AppContext,
    activity_id: int,
    at: dt.datetime,
    client: strava_write.ClientFactory | None,
) -> RideTick:
    """Write one reported ride's RIDE.LOG to Strava and record the outcome; never raises.

    A second failure in a row is final (``gave_up``, macOS notice); the email is never affected.
    """
    previous = ride_report.strava_status(ctx, activity_id)
    error: str | None = None
    try:
        # Route lookup and poem (each behind its flag; never raises for their failures).
        ride_story.prepare(ctx, activity_id)
        result = strava_write.auto_push(ctx, activity_id, client_factory=client)
        written = result is not None and result.written
        status = ride_report.STRAVA_WRITTEN if written else ride_report.STRAVA_UNCHANGED
    except Exception as exc:  # noqa: BLE001 - recorded on the row; the email already went out
        error = f"{type(exc).__name__}: {exc}"
        retried = previous == ride_report.STRAVA_FAILED
        status = ride_report.STRAVA_GAVE_UP if retried else ride_report.STRAVA_FAILED
    ride_report.record_strava(ctx, activity_id, status, error, at)
    if error is None:
        log.info("watch.strava", activity_id=activity_id, status=status)
    else:
        log.warning("watch.strava", activity_id=activity_id, status=status, error=error)
    if status == ride_report.STRAVA_GAVE_UP:
        notify_failure(ctx.settings, f"Strava RIDE.LOG {activity_id} not written: {error}")
    return RideTick(activity_id, f"strava_{status}", error or "")


def _sync(ctx: AppContext, sync: SyncFn, enabled: bool) -> str | None:
    """Run the light sync; its error (if any) fails the row after the rides were handled."""
    if not enabled:
        return None
    if not ctx.settings.intervals_api_key.get_secret_value():
        return "sync: INTERVALS_API_KEY is not set"
    try:
        sync(ctx)
    except CypError as exc:
        log.error("watch.sync_failed", error=str(exc))
        return f"sync: {exc}"
    return None


def _light_sync(ctx: AppContext) -> dict[str, Any]:
    """intervals.icu activities (+ streams) and calendar events; no Strava.

    Events too, so a workout the athlete dragged to another day in icu is seen before the
    ride's report judges the ride against it.
    """
    from cyp.jobs.sync import build_intervals_syncer

    with ctx.write_lock:
        syncer = build_intervals_syncer(ctx.settings, ctx.factory, streams=True)
        try:
            return dict(syncer.run(("activities", "events")).stages)
        finally:
            close = getattr(syncer, "close", None)
            if callable(close):
                close()


def _due(ctx: AppContext, now: dt.datetime, report: WatchReport) -> list[tuple[int, str]]:
    """Rides to send now; waiting rides are added to ``report`` and stale ones given up."""
    since = to_local(now, ctx.settings.cyp_timezone).date() - dt.timedelta(days=WINDOW_DAYS - 1)
    with ctx.factory() as s:
        rows = s.execute(
            select(Activity.id, Activity.pending_streams)
            .outerjoin(RideReport, RideReport.activity_id == Activity.id)
            .where(Activity.is_ride.is_(True), Activity.manual.is_(False))
            .where(
                or_(
                    Activity.start_local >= since.isoformat(),
                    RideReport.status.in_((ride_report.PENDING, ride_report.FAILED)),
                )
            )
            .order_by(Activity.start_local)
        ).all()
    due: list[tuple[int, str]] = []
    for activity_id, pending_streams in rows:
        seen = ride_report.observe(ctx, activity_id, now)
        if seen.status in ride_report.DONE:
            continue
        has_streams = ctx.store.exists(activity_id) or not pending_streams
        fb = ride_feedback.get(ctx, activity_id)
        rated = fb.rpe is not None or fb.feel is not None
        decision = decide(seen.first_seen_at, now, has_streams=has_streams, rated=rated)
        if decision == "send":
            due.append((activity_id, "" if has_streams else "sent without streams"))
        else:
            report.rides.append(RideTick(activity_id, decision))
    return due


def _record_skip(ctx: AppContext) -> None:
    """``job_runs`` row for a tick that found the lock held (best effort: needs a schema)."""
    try:
        ctx.check_schema()
        with job_run(JOB, ctx.factory, log_path=str(ctx.settings.logs_dir / "cyp.jsonl")) as run:
            run.incr("skipped_locked")
    except Exception:  # noqa: BLE001 - best effort; the lock holder is mid-run
        log.warning("watch.skip_not_recorded", profile=ctx.settings.cyp_profile)


def watch_all(slugs: Sequence[str]) -> list[ChildResult]:
    """Every profile's tick in its own process, like ``run --all``."""
    return run_profiles(
        slugs, command=("watch", "--json"), cwd=Path.cwd(), timeout_s=CHILD_TIMEOUT_S
    )


def spawn_tick(settings: Settings) -> None:
    """Start this profile's tick in the background and return at once (the web rating save).

    Raises:
        OSError: the process could not be started.
    """
    slug = settings.cyp_profile
    cmd = [sys.executable, "-m", "cyp.cli", *(["--profile", slug] if slug else []), "watch"]
    env = {**os.environ, "CYP_PROFILES_DIR": str(settings.cyp_profiles_dir.resolve())}
    subprocess.Popen(
        cmd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
