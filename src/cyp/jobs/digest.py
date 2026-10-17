"""Digest job: build and email every due weekly / monthly digest once (docs/specs/digest-reports).

A launch agent runs ``cyp digest run --all`` daily at 07:00 (after the 05:30 autopilot, so next
week's plan is stored). One run for one profile::

    flags (report.digest_weekly / report.digest_monthly; both off -> quiet skip, no row)
      -> profile lock (waits for a short holder, then skips with a job_runs row)
      -> for each enabled kind: every period due and not done -> build + email + row

Due times (profile time zone): a weekly digest for the ISO week Monday..Sunday is due from the
next Monday 07:00; a monthly one from the 1st 07:00 of the next month. A Mac asleep at 07:00
runs the agent on wake and catches up on every period due within :data:`CATCH_UP_DAYS`.

No surprise backfill: the first run that sees a kind's flag on records that local date
(``schema_meta``), and only periods due on or after it are sent automatically. Older periods
are sent by hand: ``cyp digest weekly --period YYYY-MM-DD --send``.

Rows live in ``digest_reports`` (one per kind + period: a period is sent once). A failed build
or email is retried by the next runs up to 3 attempts, then a macOS notice. The job never
writes a calendar or Strava.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from cyp.core.errors import ConfigError
from cyp.core.timeutil import iso_utc, now_utc, to_local
from cyp.jobs.runner import ChildResult, RunLockedError, notify_failure, profile_lock, run_profiles
from cyp.logging import get_logger
from cyp.services import digest
from cyp.services.context import AppContext
from cyp.services.digest import KINDS, Kind
from cyp.services.ride_report import Mailer
from cyp.settings import Settings
from cyp.store.migrate import upgrade_head
from cyp.store.models import SchemaMeta
from cyp.store.runs import job_run

log = get_logger(__name__)

JOB = "digest"
#: Local wall-clock time from which a period that just ended is due.
DUE_TIME = dt.time(7, 0)
#: Periods due longer ago than this are not caught up automatically (a flag switched back on
#: after months must not send a pile of old digests; the CLI backfills on purpose).
CATCH_UP_DAYS = 14
#: How long a run waits for a short holder of the lock (a ``cyp watch`` tick, the autopilot).
LOCK_WAIT_S = 20 * 60
CHILD_TIMEOUT_S = 50 * 60
#: ``schema_meta`` key holding the local date the job first saw ``kind``'s flag on.
ANCHOR_KEY = "digest.{kind}.first_enabled"

RunStatus = Literal["ok", "skipped", "failed"]


@dataclass
class DigestTick:
    """What one run did with one period."""

    kind: str
    period_start: str
    action: str  # built | sent | failed | gave_up
    detail: str = ""


@dataclass
class DigestRunReport:
    """Outcome of one profile's run (``cyp digest run --json`` prints it)."""

    profile: str
    status: RunStatus
    detail: str = ""
    digests: list[DigestTick] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """The run ran (or was skipped) and no digest failed."""
        return self.status != "failed" and all(
            d.action not in (digest.FAILED, digest.GAVE_UP) for d in self.digests
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "profile": self.profile,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "digests": [asdict(d) for d in self.digests],
        }


def due_at(kind: Kind, start: dt.date, tz: ZoneInfo) -> dt.datetime:
    """Aware local time from which the period starting ``start`` is due."""
    day = digest.period_end(kind, start) + dt.timedelta(days=1)
    return dt.datetime.combine(day, DUE_TIME, tzinfo=tz)


def latest_due(kind: Kind, now: dt.datetime, tz: ZoneInfo) -> dt.date:
    """Start of the newest period that is due at ``now``."""
    local = to_local(now, tz.key)
    today = local.date()
    if kind == "weekly":
        start = today - dt.timedelta(days=today.weekday() + 7)
    else:
        start = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    if due_at(kind, start, tz) > local:  # before 07:00 on the due day
        start = _previous(kind, start)
    return start


def due_periods(kind: Kind, now: dt.datetime, tz: ZoneInfo, *, since: dt.date) -> list[dt.date]:
    """Starts of every period due at ``now`` whose due day is ``since`` or later, oldest first.

    The window never reaches back more than :data:`CATCH_UP_DAYS` before ``now``.
    """
    floor = max(since, to_local(now, tz.key).date() - dt.timedelta(days=CATCH_UP_DAYS))
    out: list[dt.date] = []
    start = latest_due(kind, now, tz)
    while due_at(kind, start, tz).date() >= floor:
        out.append(start)
        start = _previous(kind, start)
    return out[::-1]


def _previous(kind: Kind, start: dt.date) -> dt.date:
    if kind == "weekly":
        return start - dt.timedelta(days=7)
    return (start - dt.timedelta(days=1)).replace(day=1)


def digest_profile(
    settings: Settings,
    *,
    now: dt.datetime | None = None,
    mailer: Mailer | None = None,
    lock_wait_s: float = LOCK_WAIT_S,
    sleep: Callable[[float], None] | None = None,
) -> DigestRunReport:
    """One run for one profile; always returns a report (see the module docstring).

    With both digest flags off it returns ``skipped`` without taking the lock or writing a row.
    """
    profile = settings.cyp_profile or "default"
    ctx = AppContext.from_settings(settings)
    try:
        try:
            kinds = [k for k in KINDS if ctx.features().enabled(digest.FLAGS[k])]
        except ConfigError as exc:
            return DigestRunReport(profile, "failed", f"config: {str(exc).splitlines()[0]}")
        if not kinds:
            return DigestRunReport(
                profile,
                "skipped",
                "features report.digest_weekly and report.digest_monthly are off",
            )
        for path in settings.data_layout:
            path.mkdir(parents=True, exist_ok=True)
        lock_kw: dict[str, Any] = {"wait_s": lock_wait_s}
        if sleep is not None:
            lock_kw["sleep"] = sleep
        with profile_lock(settings.cyp_data_dir, **lock_kw):
            upgrade_head(settings.cyp_db_url)
            return run(ctx, kinds, now=now, mailer=mailer)
    except RunLockedError:
        _record_skip(ctx)
        return DigestRunReport(profile, "skipped", "another run holds the profile lock")
    except Exception as exc:  # the scheduler must always get a report
        log.exception("digest.setup_failed", profile=profile)
        return DigestRunReport(profile, "failed", f"setup: {type(exc).__name__}: {exc}")
    finally:
        ctx.close()


def run(
    ctx: AppContext,
    kinds: Sequence[Kind],
    *,
    now: dt.datetime | None = None,
    mailer: Mailer | None = None,
) -> DigestRunReport:
    """The body of a run, under the caller's lock. One ``job_runs`` row (``digest``)."""
    at = now or now_utc()
    tz = ctx.tz
    report = DigestRunReport(ctx.settings.cyp_profile or "default", "ok")
    with job_run(JOB, ctx.factory, log_path=str(ctx.settings.logs_dir / "cyp.jsonl")) as job:
        for kind in kinds:
            since = _anchor(ctx, kind, to_local(at, tz.key).date(), at)
            for start in due_periods(kind, at, tz, since=since):
                if digest.status(ctx, kind, start) in digest.DONE:
                    continue
                out = digest.send(ctx, kind, start, mailer=mailer, now=at)
                report.digests.append(
                    DigestTick(kind, start.isoformat(), out.status, out.error or "")
                )
                job.incr(out.status)
                if out.status == digest.GAVE_UP:
                    notify_failure(ctx.settings, f"{kind} digest {start} not sent: {out.error}")
    return report


def _anchor(ctx: AppContext, kind: Kind, today: dt.date, now: dt.datetime) -> dt.date:
    """The local date the job first saw ``kind`` on; recorded as ``today`` the first time."""
    key = ANCHOR_KEY.format(kind=kind)
    with ctx.write_lock, ctx.factory() as s:
        row = s.get(SchemaMeta, key)
        if row is None or not row.value:
            row = SchemaMeta(key=key, value=today.isoformat(), updated_at=iso_utc(now))
            s.merge(row)
            s.commit()
            log.info("digest.first_enabled", kind=kind, date=today.isoformat())
            return today
        return dt.date.fromisoformat(row.value)


def _record_skip(ctx: AppContext) -> None:
    """``job_runs`` row for a run that found the lock held (best effort: needs a schema)."""
    try:
        ctx.check_schema()
        with job_run(JOB, ctx.factory, log_path=str(ctx.settings.logs_dir / "cyp.jsonl")) as job:
            job.incr("skipped_locked")
    except Exception:  # noqa: BLE001 - best effort; the lock holder is mid-run
        log.warning("digest.skip_not_recorded", profile=ctx.settings.cyp_profile)


def digest_all(slugs: Sequence[str]) -> list[ChildResult]:
    """Every profile's run in its own process, like ``run --all``."""
    return run_profiles(
        slugs, command=("digest", "run", "--json"), cwd=Path.cwd(), timeout_s=CHILD_TIMEOUT_S
    )
