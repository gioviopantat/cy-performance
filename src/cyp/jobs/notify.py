"""Notifications: macOS notices and the ride-report email.

``notify.macos`` shows a notice when a scheduled run fails; ``notify.email`` sends the per-ride
PDF (docs/specs/ride-report.md).
"""

from __future__ import annotations

import mimetypes
import smtplib
import subprocess
import sys
from collections.abc import Callable, Sequence
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from pathlib import Path

from cyp.core.errors import ConfigError, CypError
from cyp.logging import get_logger
from cyp.settings import Settings

log = get_logger(__name__)


def applescript(title: str, message: str) -> str:
    """AppleScript for a notification; quotes and backslashes escaped."""

    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"display notification {q(message)} with title {q(title)}"


def notify_macos(title: str, message: str) -> bool:
    """Show a macOS notification; ``False`` (no error) elsewhere or when it fails."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", applescript(title, message)],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notify.failed", error=str(exc))
        return False
    return True


SMTP_TIMEOUT_S = 30
#: Port of implicit TLS; every other port connects in plain text and upgrades with STARTTLS.
SMTPS_PORT = 465


class EmailError(CypError):
    """The SMTP server refused or could not be reached."""


#: ``(host, port, timeout) -> client``; tests pass a fake.
SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


def _smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    if port == SMTPS_PORT:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.starttls()
    except BaseException:
        client.close()
        raise
    return client


def cc_recipients(settings: Settings) -> list[tuple[str, str]]:
    """``CYP_REPORT_EMAIL_CC`` as ``(name, address)`` pairs (``Name <addr>`` or ``addr``)."""
    return [(n, a) for n, a in getaddresses([settings.cyp_report_email_cc]) if a]


def send_email(
    settings: Settings,
    subject: str,
    body_text: str,
    attachments: Sequence[Path] = (),
    *,
    html: str | None = None,
    smtp: SmtpFactory = _smtp,
) -> None:
    """Send one email to ``CYP_REPORT_EMAIL_TO`` (copies to ``CYP_REPORT_EMAIL_CC``) over TLS.

    Recipients come only from this profile's settings. Logs host and port, never the
    password.

    Raises:
        ConfigError: SMTP host, user, password or recipient not set.
        EmailError: connecting, logging in or sending failed.
    """
    host, user = settings.cyp_smtp_host, settings.cyp_smtp_user
    password = settings.cyp_smtp_password.get_secret_value()
    to = settings.cyp_report_email_to
    missing = [
        name
        for name, value in (
            ("CYP_SMTP_HOST", host),
            ("CYP_SMTP_USER", user),
            ("CYP_SMTP_PASSWORD", password),
            ("CYP_REPORT_EMAIL_TO", to),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"email not configured: set {', '.join(missing)} in the profile's .env")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    cc = cc_recipients(settings)
    if cc:
        msg["Cc"] = ", ".join(formataddr(x) for x in cc)
    msg.set_content(body_text)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for path in attachments:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    port = settings.cyp_smtp_port
    try:
        with smtp(host, port, SMTP_TIMEOUT_S) as client:
            client.login(user, password)
            client.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email.failed", host=host, port=port, error=type(exc).__name__)
        raise EmailError(f"email via {host}:{port} failed: {type(exc).__name__}: {exc}") from exc
    log.info("email.sent", host=host, port=port, attachments=len(attachments), cc=len(cc))
