"""Build and deliver one weekly / monthly digest PDF (docs/specs/digest-reports.md).

``build`` runs the contract chain (facts -> PDF) and touches no row. ``send`` builds, emails
when ``notify.email`` is on, and records the outcome in ``digest_reports`` (one row per kind and
period: the dedupe). Neither writes a calendar or Strava. ``cyp digest`` and the 07:00 job
(:mod:`cyp.jobs.digest`) call this.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from cyp.core.errors import ConfigError
from cyp.core.timeutil import iso_utc, now_utc, to_local
from cyp.logging import get_logger
from cyp.reports import email_copy
from cyp.reports.digest_facts import build_monthly, build_weekly
from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest
from cyp.reports.digest_pdf import render_digest_pdf
from cyp.services import adaptive
from cyp.services.context import AppContext
from cyp.services.ride_report import Mailer, recipient_name
from cyp.store.models import DigestReport

log = get_logger(__name__)

Kind = Literal["weekly", "monthly"]
KINDS: tuple[Kind, ...] = ("weekly", "monthly")
FLAGS: dict[Kind, str] = {
    "weekly": "report.digest_weekly",
    "monthly": "report.digest_monthly",
}

BUILT = "built"
SENT = "sent"
FAILED = "failed"
GAVE_UP = "gave_up"
#: Terminal states: the job never touches these periods again.
DONE = (BUILT, SENT, GAVE_UP)
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class Outcome:
    """What ``send`` did for one period."""

    kind: Kind
    period_start: dt.date
    status: str
    attempts: int
    pdf_path: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        """Built, and emailed when email is on."""
        return self.status in (BUILT, SENT)


def digest_dir(ctx: AppContext) -> Path:
    """``data/reports/digest`` (git-ignored, inside the profile's data dir)."""
    return ctx.reports_dir / "digest"


def check_period(kind: Kind, start: dt.date) -> None:
    """A weekly period starts on a Monday (ISO week), a monthly one on day 1.

    Raises:
        ConfigError: ``start`` is not the first day of a ``kind`` period.
    """
    if kind == "weekly" and start.weekday() != 0:
        raise ConfigError(f"weekly period must start on a Monday, got {start} ({start:%a})")
    if kind == "monthly" and start.day != 1:
        raise ConfigError(f"monthly period must start on day 1, got {start}")


def period_end(kind: Kind, start: dt.date) -> dt.date:
    """Last day (inclusive) of the period starting ``start``."""
    if kind == "weekly":
        return start + dt.timedelta(days=6)
    return next_month(start) - dt.timedelta(days=1)


def next_month(start: dt.date) -> dt.date:
    """Day 1 of the month after ``start``'s."""
    return dt.date(start.year + start.month // 12, start.month % 12 + 1, 1)


def build(ctx: AppContext, kind: Kind, period_start: dt.date) -> Path:
    """Write the period's PDF (and its facts JSON) and return the PDF path.

    Raises:
        ConfigError: the kind's feature is off, or ``period_start`` is not a period start.
    """
    _require(ctx, FLAGS[kind])
    check_period(kind, period_start)
    common: dict[str, Any] = {
        "profile": ctx.settings.cyp_profile or "default",
        "cfg": ctx.athlete_config_or_none(),
        "reports_dir": ctx.reports_dir,
    }
    with ctx.factory() as session:
        digest: WeeklyDigest | MonthlyDigest
        if kind == "weekly":
            # Why next week is planned so (flag plan.progression_gate, ADR-0018).
            gate = adaptive.gate_decision(ctx, period_start + dt.timedelta(days=7))
            digest = build_weekly(session, ctx.store, period_start, gate=gate, **common)
        else:
            digest = build_monthly(session, ctx.store, period_start, **common)
    return render_digest_pdf(digest, digest_dir(ctx))


def send(
    ctx: AppContext,
    kind: Kind,
    period_start: dt.date,
    *,
    mailer: Mailer | None = None,
    now: dt.datetime | None = None,
) -> Outcome:
    """Build, email (if ``notify.email``) and record the outcome; never raises for a failure.

    A failed build or email is ``failed`` (the next run retries it) until the
    :data:`MAX_ATTEMPTS`-th failure, then ``gave_up``. Calling it for a period already sent
    sends it again (``cyp digest weekly --send``).

    Raises:
        ConfigError: the kind's feature is off, ``period_start`` is not a period start, or the
            period has not ended yet (a digest of a running week is never emailed).
    """
    _require(ctx, FLAGS[kind])
    check_period(kind, period_start)
    at = now or now_utc()
    if period_end(kind, period_start) >= to_local(at, ctx.settings.cyp_timezone).date():
        raise ConfigError(f"the {kind} period starting {period_start} has not ended yet")
    email = ctx.features().enabled("notify.email")
    path: Path | None = None
    error: str | None = None
    try:
        path = build(ctx, kind, period_start)
        if email:
            _deliver(ctx, kind, period_start, path, mailer)
    except Exception as exc:  # recorded on the row; the next run retries
        log.exception("digest.failed", kind=kind, period_start=period_start.isoformat())
        error = f"{type(exc).__name__}: {exc}"
    with ctx.write_lock, ctx.factory() as s:
        row = _row(s, kind, period_start, at)
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
        return Outcome(kind, period_start, row.status, row.attempts, row.pdf_path, row.error)


def status(ctx: AppContext, kind: Kind, period_start: dt.date) -> str | None:
    """The period's row status, ``None`` when it was never attempted."""
    with ctx.factory() as s:
        row = s.get(DigestReport, (kind, period_start))
        return row.status if row is not None else None


def _row(s: Session, kind: Kind, period_start: dt.date, now: dt.datetime) -> DigestReport:
    row = s.get(DigestReport, (kind, period_start))
    if row is None:
        row = DigestReport(
            kind=kind, period_start=period_start, status=FAILED, attempts=0, updated_at=iso_utc(now)
        )
        s.add(row)
        s.flush()
    return row


def _deliver(
    ctx: AppContext, kind: Kind, period_start: dt.date, pdf: Path, mailer: Mailer | None
) -> None:
    if mailer is None:
        from cyp.jobs.notify import send_email

        mailer = send_email
    from cyp.jobs.notify import cc_recipients

    digest = load_digest(kind, pdf)
    to_name = recipient_name(ctx, digest.athlete_name)
    cc_names = [n or a for n, a in cc_recipients(ctx.settings)]
    if isinstance(digest, WeeklyDigest):
        content = email_copy.compose_weekly(
            digest, to_name=to_name, cc_names=cc_names, today=ctx.today()
        )
    else:
        content = email_copy.compose_monthly(
            digest, to_name=to_name, cc_names=cc_names, today=ctx.today()
        )
    mailer(ctx.settings, content.subject, content.text, [pdf], html=content.html)


def load_digest(kind: Kind, pdf: Path) -> WeeklyDigest | MonthlyDigest:
    """The facts the PDF was rendered from (the ``.json`` written beside it)."""
    model = WeeklyDigest if kind == "weekly" else MonthlyDigest
    return model.model_validate_json(pdf.with_suffix(".json").read_text(encoding="utf-8"))


def _require(ctx: AppContext, flag: str) -> None:
    if not ctx.features().enabled(flag):
        raise ConfigError(
            f"feature {flag} is off for this profile (athlete.yaml `features: {{{flag}: true}}`)"
        )
