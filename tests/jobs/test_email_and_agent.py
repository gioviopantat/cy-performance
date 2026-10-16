"""SMTP email, its per-profile settings, the watch launch agent and the CLI entry points."""

from __future__ import annotations

import plistlib
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from cyp.cli import app
from cyp.core.errors import ConfigError
from cyp.jobs import notify
from cyp.jobs.launchd import WATCH_LABEL, AgentSpec, build_plist, watch_spec
from cyp.jobs.notify import EmailError, send_email
from cyp.settings import Settings, profile_settings

SECRET = "app-password-123"


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "cyp_smtp_host": "smtp.example.test",
        "cyp_smtp_port": 587,
        "cyp_smtp_user": "rider@example.test",
        "cyp_smtp_password": SECRET,
        "cyp_report_email_to": "coach@example.test",
    }
    return Settings(**{**base, **kw})


class FakeSmtp:
    """Records the session; ``fail_login`` raises like a wrong App Password."""

    instances: ClassVar[list[FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float = 0, fail_login: bool = False) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        self.fail_login = fail_login
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def close(self) -> None:
        self.calls.append("close")

    def login(self, user: str, password: str) -> None:
        if self.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login:{user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def test_send_email_with_attachment(tmp_path: Path) -> None:
    pdf = tmp_path / "2026-10-05_1.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    clients: list[FakeSmtp] = []

    def factory(host: str, port: int, timeout: float) -> Any:
        clients.append(FakeSmtp(host, port, timeout))
        return clients[-1]

    send_email(_settings(), "\u9a0e\u4e58", "body", [pdf], smtp=factory)
    (client,) = clients
    assert (client.host, client.port) == ("smtp.example.test", 587)
    assert client.calls == ["login:rider@example.test", "quit"]
    msg = client.sent[0]
    assert (msg["To"], msg["From"], msg["Subject"]) == (
        "coach@example.test",
        "rider@example.test",
        "\u9a0e\u4e58",
    )
    (att,) = list(msg.iter_attachments())
    assert att.get_filename() == pdf.name and att.get_content_type() == "application/pdf"


def test_tls_mode_follows_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", lambda h, p, timeout: FakeSmtp(h, p, timeout))
    send_email(_settings(cyp_smtp_port=587), "s", "b")
    send_email(_settings(cyp_smtp_port=465), "s", "b")
    starttls, ssl = FakeSmtp.instances
    assert starttls.calls[0] == "starttls" and "starttls" not in ssl.calls


def test_missing_settings_and_smtp_errors_never_show_the_password(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ConfigError, match="CYP_SMTP_PASSWORD, CYP_REPORT_EMAIL_TO"):
        send_email(_settings(cyp_smtp_password="", cyp_report_email_to=""), "s", "b")

    def factory(host: str, port: int, timeout: float) -> Any:
        return FakeSmtp(host, port, timeout, fail_login=True)

    with pytest.raises(EmailError) as err:
        send_email(_settings(), "s", "b", smtp=factory)
    assert "SMTPAuthenticationError" in str(err.value)
    assert SECRET not in str(err.value) and SECRET not in capsys.readouterr().err
    assert SECRET not in str(_settings().masked_summary())


def test_smtp_settings_come_only_from_the_profile_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "profiles" / "dad"
    root.mkdir(parents=True)
    (root / ".env").write_text("CYP_SMTP_HOST=smtp.gmail.com\nCYP_REPORT_EMAIL_TO=dad@x.test\n")
    for key, value in {
        "CYP_SMTP_HOST": "leak.test",
        "CYP_SMTP_USER": "leak@x.test",
        "CYP_SMTP_PASSWORD": "leaked",
        "CYP_REPORT_EMAIL_TO": "leak@x.test",
        "CYP_SMTP_PORT": "2525",
    }.items():
        monkeypatch.setenv(key, value)
    s = profile_settings("dad", tmp_path / "profiles")
    assert (s.cyp_smtp_host, s.cyp_report_email_to) == ("smtp.gmail.com", "dad@x.test")
    assert (s.cyp_smtp_user, s.cyp_smtp_password.get_secret_value(), s.cyp_smtp_port) == (
        "",
        "",
        587,
    )


def test_watch_agent_plist(tmp_path: Path) -> None:
    spec = watch_spec(tmp_path)
    plist = plistlib.loads(build_plist(AgentSpec(**{**spec.__dict__, "python": "/v/python"})))
    assert plist["Label"] == WATCH_LABEL == "com.cy-performance.watch"
    assert plist["ProgramArguments"] == ["/v/python", "-m", "cyp.cli", "watch", "--all"]
    assert plist["StartInterval"] == 600 and "StartCalendarInterval" not in plist
    assert spec.log_path.name == "watch.log"
    autopilot = plistlib.loads(build_plist(AgentSpec(workdir=tmp_path)))
    assert "StartInterval" not in autopilot and autopilot["Label"] == "com.cy-performance.autopilot"
    assert AgentSpec(workdir=tmp_path).log_path.name == "autopilot.log"
    with pytest.raises(ValueError):
        build_plist(watch_spec(tmp_path, interval_s=10))


def test_cli_watch_with_the_flag_off_is_a_quiet_skip() -> None:
    res = CliRunner().invoke(app, ["watch", "--json"])
    assert res.exit_code == 0, res.output
    assert '"status": "skipped"' in res.output and "report.ride is off" in res.output


def test_cli_ride_report_needs_the_flag(settings: Settings) -> None:
    from cyp.store.migrate import upgrade_head

    upgrade_head(settings.cyp_db_url)
    res = CliRunner().invoke(app, ["ride-report", "1"])
    assert res.exit_code == 1 and "report.ride is off" in res.output
