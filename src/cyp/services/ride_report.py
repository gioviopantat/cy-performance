"""Build and deliver one ride's PDF report (docs/specs/ride-report.md); CLI and watcher call this.

``build`` runs the contract chain (tomorrow's dry-run preview -> facts -> PDF) and touches no
row. ``send`` builds, emails when ``notify.email`` is on, and records the outcome in
``ride_reports`` (one row per ride: the dedupe). Neither writes a calendar or Strava.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cyp.core.errors import ConfigError, NotFoundError
from cyp.core.timeutil import iso_utc, now_utc, parse_iso
from cyp.logging import get_logger
from cyp.reports import email_copy
from cyp.reports.ride_facts import build_ride_report
from cyp.reports.ride_model import RideReport as RideReportFacts
from cyp.reports.ride_pdf import render_ride_pdf
from cyp.services import ride_feedback
from cyp.services.context import AppContext
from cyp.services.ride_preview import tomorrow_preview
from cyp.store.models import Activity, RideReport

log = get_logger(__name__)

#: Rides older than this (local days before today) get no section 8 tomorrow preview.
PREVIEW_MAX_AGE_DAYS = 1

PENDING = "pending_feel"
BUILT = "built"
SENT = "sent"
FAILED = "failed"
GAVE_UP = "gave_up"
#: Terminal states: the watcher never builds or sends these rides again.
DONE = (BUILT, SENT, GAVE_UP)
MAX_ATTEMPTS = 3
ICU_ACTIVITY_URL = "https://intervals.icu/activities/{id}"

#: ``(settings, subject, body_text, attachments, *, html=...)``; :func:`cyp.jobs.notify.send_email`.
Mailer = Callable[..., None]


@dataclass(frozen=True)
class Outcome:
    """What ``send`` did for one ride."""

    activity_id: int
    status: str
    attempts: int
    pdf_path: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        """Built, and emailed when email is on."""
        return self.status in (BUILT, SENT)


def ride_dir(ctx: AppContext) -> Path:
    """``data/reports/ride`` (git-ignored, inside the profile's data dir)."""
    return ctx.reports_dir / "ride"


def build(ctx: AppContext, activity_id: int) -> Path:
    """Write the ride's PDF (and its facts JSON) and return the PDF path.

    Raises:
        ConfigError: feature ``report.ride`` is off. NotFoundError: unknown activity.
    """
    _require(ctx, "report.ride")
    day, icu_id, _name = _ride(ctx, activity_id)
    # A preview is only meaningful while the next day is still ahead: for an older ride (a
    # resend or a backfill) today's planner would describe a day that already happened.
    fresh = day >= ctx.today() - dt.timedelta(days=PREVIEW_MAX_AGE_DAYS)
    tomorrow = tomorrow_preview(ctx, day + dt.timedelta(days=1)) if fresh else None
    rate_url = ICU_ACTIVITY_URL.format(id=icu_id) if icu_id else None
    fb = ride_feedback.get(ctx, activity_id)
    with ctx.factory() as session:
        report = build_ride_report(
            session,
            ctx.store,
            activity_id,
            profile=ctx.settings.cyp_profile or "default",
            cfg=ctx.athlete_config_or_none(),
            tomorrow=tomorrow,
            rate_url=rate_url,
            feedback=(fb.rpe, fb.feel),
            casual=fb.casual,
        )
    return render_ride_pdf(report, ride_dir(ctx))


def send(
    ctx: AppContext,
    activity_id: int,
    *,
    mailer: Mailer | None = None,
    now: dt.datetime | None = None,
) -> Outcome:
    """Build, email (if ``notify.email``) and record the outcome; never raises for a failure.

    A failed build or email is ``failed`` (the watcher retries it) until the
    :data:`MAX_ATTEMPTS`-th failure, then ``gave_up``. Calling it for a ride already sent sends
    it again (``cyp ride-report --send``).

    Raises:
        ConfigError: feature ``report.ride`` is off. NotFoundError: unknown activity.
    """
    _require(ctx, "report.ride")
    _ride(ctx, activity_id)
    at = now or now_utc()
    email = ctx.features().enabled("notify.email")
    path: Path | None = None
    error: str | None = None
    try:
        path = build(ctx, activity_id)
        if email:
            _deliver(ctx, activity_id, path, mailer)
    except Exception as exc:  # recorded on the row; the watcher retries
        log.exception("ride_report.failed", activity_id=activity_id)
        error = f"{type(exc).__name__}: {exc}"
    with ctx.write_lock, ctx.factory() as s:
        row = _row(s, activity_id, at)
        row.attempts = (row.attempts or 0) + 1
        row.updated_at = iso_utc(at)
        if path is not None:
            row.pdf_path = str(path)
        if error is None:
            row.status = SENT if email else BUILT
            row.error = None
            if email:
                row.sent_at = iso_utc(at)
        else:
            row.status = GAVE_UP if row.attempts >= MAX_ATTEMPTS else FAILED
            row.error = error[-2000:]
        s.commit()
        return Outcome(activity_id, row.status, row.attempts, row.pdf_path, row.error)


@dataclass(frozen=True)
class Seen:
    """A ride's ``ride_reports`` row as the watcher needs it."""

    status: str
    attempts: int
    first_seen_at: dt.datetime


def observe(ctx: AppContext, activity_id: int, now: dt.datetime) -> Seen:
    """The ride's row, created as ``pending_feel`` the first time the watcher sees the ride."""
    with ctx.write_lock, ctx.factory() as s:
        row = _row(s, activity_id, now)
        seen = Seen(row.status, row.attempts or 0, parse_iso(row.first_seen_at))
        s.commit()
        return seen


#: ``ride_reports.strava_status`` values (flag ``strava.auto_description``, ADR-0020).
STRAVA_WRITTEN = "written"
STRAVA_UNCHANGED = "unchanged"
STRAVA_FAILED = "failed"
STRAVA_GAVE_UP = "gave_up"


def strava_status(ctx: AppContext, activity_id: int) -> str | None:
    """The ride's last automatic Strava outcome (``None``: never tried or no row)."""
    with ctx.factory() as s:
        row = s.get(RideReport, activity_id)
        return row.strava_status if row is not None else None


def strava_retries(ctx: AppContext) -> list[int]:
    """Rides whose report is done and whose one automatic Strava write failed once."""
    with ctx.factory() as s:
        return list(
            s.scalars(
                select(RideReport.activity_id)
                .where(RideReport.status.in_((BUILT, SENT)))
                .where(RideReport.strava_status == STRAVA_FAILED)
                .order_by(RideReport.activity_id)
            )
        )


def record_strava(
    ctx: AppContext, activity_id: int, status: str, error: str | None, now: dt.datetime
) -> None:
    """Store the automatic Strava outcome on the ride's row (the report status is untouched)."""
    with ctx.write_lock, ctx.factory() as s:
        row = _row(s, activity_id, now)
        row.strava_status = status
        row.strava_error = error[-2000:] if error else None
        row.updated_at = iso_utc(now)
        s.commit()


def mark_gave_up(ctx: AppContext, activity_id: int, reason: str, now: dt.datetime) -> None:
    """Stop retrying a ride (its row stays as evidence)."""
    with ctx.write_lock, ctx.factory() as s:
        row = _row(s, activity_id, now)
        row.status, row.error, row.updated_at = GAVE_UP, reason, iso_utc(now)
        s.commit()


def _row(s: Session, activity_id: int, now: dt.datetime) -> RideReport:
    row = s.get(RideReport, activity_id)
    if row is None:
        row = RideReport(
            activity_id=activity_id,
            status=PENDING,
            attempts=0,
            first_seen_at=iso_utc(now),
            updated_at=iso_utc(now),
        )
        s.add(row)
        s.flush()
    return row


def _deliver(ctx: AppContext, activity_id: int, pdf: Path, mailer: Mailer | None) -> None:
    if mailer is None:
        from cyp.jobs.notify import send_email

        mailer = send_email
    from cyp.jobs.notify import cc_recipients

    report = load_report(pdf)
    content = email_copy.compose_ride(
        report,
        to_name=recipient_name(ctx, report.athlete_name),
        cc_names=[n or a for n, a in cc_recipients(ctx.settings)],
        today=ctx.today(),
    )
    mailer(ctx.settings, content.subject, content.text, [pdf], html=content.html)


def load_report(pdf: Path) -> RideReportFacts:
    """The facts the PDF was rendered from (the ``.json`` written beside it)."""
    return RideReportFacts.model_validate_json(pdf.with_suffix(".json").read_text(encoding="utf-8"))


def recipient_name(ctx: AppContext, athlete_name: str) -> str:
    """How report emails greet this profile's recipient.

    ``CYP_REPORT_EMAIL_NAME``, else the athlete's first name as intervals.icu has it.
    """
    if ctx.settings.cyp_report_email_name:
        return ctx.settings.cyp_report_email_name
    return athlete_name.split()[0] if athlete_name.strip() else "你"


def _ride(ctx: AppContext, activity_id: int) -> tuple[dt.date, str | None, str]:
    """``(local day, intervals.icu id, name)``.

    Raises:
        NotFoundError: unknown activity.
    """
    with ctx.factory() as s:
        a = s.get(Activity, activity_id)
        if a is None:
            raise NotFoundError(f"no activity {activity_id}")
        start = a.start_local or a.start_utc
        return dt.date.fromisoformat(start[:10]), a.intervals_id, a.name or ""


def _require(ctx: AppContext, flag: str) -> None:
    if not ctx.features().enabled(flag):
        raise ConfigError(
            f"feature {flag} is off for this profile (athlete.yaml `features: {{{flag}: true}}`)"
        )
