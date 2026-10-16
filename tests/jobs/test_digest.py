"""Digest job: due times, catch-up, no surprise backfill, dedupe, retries, lock, flags, CLI.

docs/specs/digest-reports.md. The builders and the renderer belong to other sessions; they are
faked here by their contract signatures.
"""

from __future__ import annotations

import datetime as dt
import plistlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from cyp.cli import app
from cyp.core.errors import ConfigError
from cyp.jobs import digest as job
from cyp.jobs.launchd import DIGEST_LABEL, AgentSpec, build_plist, digest_spec
from cyp.jobs.notify import EmailError
from cyp.jobs.runner import profile_lock
from cyp.reports import email_copy
from cyp.reports.digest_model import DigestHeadline, MonthlyDigest, Period, WeeklyDigest
from cyp.services import digest
from cyp.services.context import AppContext
from cyp.settings import Settings
from cyp.store.models import DigestReport, JobRun, SchemaMeta
from tests.api.conftest import make_ctx

TZ = ZoneInfo("Asia/Taipei")
ON = "notify.macos=off,report.digest_weekly=on,report.digest_monthly=on,notify.email=on"


def local(*args: int) -> dt.datetime:
    """Aware Asia/Taipei time (the job converts to the profile zone itself)."""
    return dt.datetime(*args, tzinfo=TZ)  # type: ignore[misc]


def d(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


class Mailer:
    """Records sends; raises ``fail`` when set."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, list[Path]]] = []
        self.html: list[str | None] = []
        self.fail = fail

    def __call__(
        self,
        settings: Settings,
        subject: str,
        body: str,
        attachments: Sequence[Path],
        *,
        html: str | None = None,
    ) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((subject, body, list(attachments)))
        self.html.append(html)


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dt.date]]:
    """Fake the contract stubs and the email copy; records ``(kind, period_start)`` per build."""
    calls: list[tuple[str, dt.date]] = []

    def fake(kind: str) -> Any:
        model = WeeklyDigest if kind == "weekly" else MonthlyDigest

        def build(
            session: Any, store: Any, start: dt.date, *, profile: str, cfg: Any, reports_dir: Path
        ) -> WeeklyDigest | MonthlyDigest:
            assert reports_dir.name == "reports"
            calls.append((kind, start))
            return model(
                profile=profile,
                athlete_name="Rider Test",
                generated_at=dt.datetime(2026, 10, 19, tzinfo=dt.UTC),
                period=Period(
                    start=start, end=digest.period_end(kind, start), label_zh=start.isoformat()
                ),
                headline=DigestHeadline(text_zh="ok", verdict="on_track"),
            )

        return build

    def fake_render(report: WeeklyDigest | MonthlyDigest, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        kind = "weekly" if isinstance(report, WeeklyDigest) else "monthly"
        path = out_dir / f"{kind}_{report.period.start.isoformat()}.pdf"
        path.write_bytes(b"%PDF-1.7 fake")
        path.with_suffix(".json").write_text(report.model_dump_json(), encoding="utf-8")
        return path

    def fake_compose(kind: str) -> Any:
        def compose(
            d: WeeklyDigest | MonthlyDigest, *, to_name: str, cc_names: Any, today: dt.date
        ) -> email_copy.EmailContent:
            subject = f"{kind} {d.period.start} to {to_name} cc {','.join(cc_names)}"
            return email_copy.EmailContent(subject, "text", "<p>html</p>")

        return compose

    monkeypatch.setattr(digest, "build_weekly", fake("weekly"))
    monkeypatch.setattr(digest, "build_monthly", fake("monthly"))
    monkeypatch.setattr(digest, "render_digest_pdf", fake_render)
    monkeypatch.setattr(email_copy, "compose_weekly", fake_compose("weekly"))
    monkeypatch.setattr(email_copy, "compose_monthly", fake_compose("monthly"))
    return calls


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(job, "notify_failure", lambda _s, msg: out.append(msg))
    return out


def _ctx(data: Path, features: str = ON) -> AppContext:
    return make_ctx(data, cyp_features=features)


@pytest.fixture
def dctx(data: Path) -> Iterator[AppContext]:
    c = _ctx(data)
    yield c
    c.close()


def _anchor(ctx: AppContext, kind: str, day: str) -> None:
    """Pretend the job first saw ``kind`` on ``day``."""
    with ctx.factory() as s:
        s.merge(SchemaMeta(key=job.ANCHOR_KEY.format(kind=kind), value=day))
        s.commit()


def _run(
    ctx: AppContext, at: dt.datetime, mailer: Mailer, kinds: Sequence[digest.Kind] = ("weekly",)
) -> job.DigestRunReport:
    return job.run(ctx, kinds, now=at, mailer=mailer)


def _row(ctx: AppContext, kind: str, start: str) -> DigestReport:
    with ctx.factory() as s:
        row = s.get(DigestReport, (kind, d(start)))
        assert row is not None
        s.expunge(row)
        return row


# ---------------------------------------------------------------------------------- due times


@pytest.mark.parametrize(
    ("now", "want"),
    [
        (local(2026, 10, 19, 6, 59), "2026-10-05"),  # Monday before 07:00: last week not due
        (local(2026, 10, 19, 7, 0), "2026-10-12"),  # Monday 07:00: due
        (local(2026, 10, 18, 23, 0), "2026-10-05"),  # Sunday night
        (local(2026, 10, 25, 12, 0), "2026-10-12"),  # Sunday of the next week
        (local(2027, 1, 4, 7, 0), "2026-12-28"),  # ISO week across the new year
        (local(2027, 1, 4, 6, 0), "2026-12-21"),
    ],
)
def test_latest_weekly(now: dt.datetime, want: str) -> None:
    assert job.latest_due("weekly", now, TZ) == d(want)


@pytest.mark.parametrize(
    ("now", "want"),
    [
        (local(2026, 11, 1, 6, 59), "2026-09-01"),
        (local(2026, 11, 1, 7, 0), "2026-10-01"),
        (local(2026, 10, 31, 23, 59), "2026-09-01"),
        (local(2027, 1, 1, 7, 0), "2026-12-01"),  # year boundary
        (local(2027, 1, 1, 6, 0), "2026-11-01"),
        (local(2027, 3, 1, 7, 0), "2027-02-01"),  # short month
    ],
)
def test_latest_monthly(now: dt.datetime, want: str) -> None:
    assert job.latest_due("monthly", now, TZ) == d(want)


def test_due_uses_the_profile_zone_not_utc() -> None:
    # 2026-10-18 23:00 UTC is Monday 07:00 in Taipei.
    assert job.latest_due("weekly", dt.datetime(2026, 10, 18, 23, 0, tzinfo=dt.UTC), TZ) == d(
        "2026-10-12"
    )
    assert digest.period_end("monthly", d("2026-02-01")) == d("2026-02-28")
    assert digest.period_end("weekly", d("2026-12-28")) == d("2027-01-03")


def test_catch_up_lists_every_missed_period_oldest_first() -> None:
    # Mac asleep for two Mondays: both weeks are due on wake.
    now = local(2026, 10, 27, 9, 0)
    assert job.due_periods("weekly", now, TZ, since=d("2026-10-01")) == [
        d("2026-10-12"),
        d("2026-10-19"),
    ]
    # ...but never more than CATCH_UP_DAYS back, whatever the anchor says.
    far = job.due_periods("weekly", local(2026, 12, 30, 9, 0), TZ, since=d("2026-01-01"))
    assert far == [d("2026-12-14"), d("2026-12-21")]
    assert job.due_periods("monthly", local(2027, 1, 2, 9, 0), TZ, since=d("2026-12-20")) == [
        d("2026-12-01")
    ]


def test_check_period() -> None:
    digest.check_period("weekly", d("2026-10-05"))
    digest.check_period("monthly", d("2026-09-01"))
    with pytest.raises(ConfigError, match="Monday"):
        digest.check_period("weekly", d("2026-10-06"))
    with pytest.raises(ConfigError, match="day 1"):
        digest.check_period("monthly", d("2026-09-02"))


# ------------------------------------------------------------------------ anchor and dedupe


def test_first_run_records_the_anchor_and_does_not_backfill(
    dctx: AppContext, built: list[Any]
) -> None:
    mailer = Mailer()
    # First run on a Wednesday: last week (due Monday) is older than the first enable.
    report = _run(dctx, local(2026, 10, 14, 7, 0), mailer, ("weekly", "monthly"))
    assert report.digests == [] and built == [] and mailer.sent == []
    with dctx.factory() as s:
        assert s.get(SchemaMeta, "digest.weekly.first_enabled").value == "2026-10-14"  # type: ignore[union-attr]
    # The next Monday's week is the first one sent.
    report = _run(dctx, local(2026, 10, 19, 7, 0), mailer)
    assert [(t.kind, t.period_start, t.action) for t in report.digests] == [
        ("weekly", "2026-10-12", "sent")
    ]


def test_first_run_on_the_due_day_sends_that_period(dctx: AppContext, built: list[Any]) -> None:
    report = _run(dctx, local(2026, 10, 19, 7, 0), Mailer())
    assert [t.period_start for t in report.digests] == ["2026-10-12"]


def test_a_period_is_sent_once(dctx: AppContext, built: list[Any]) -> None:
    _anchor(dctx, "weekly", "2026-10-15")
    mailer = Mailer()
    _run(dctx, local(2026, 10, 19, 7, 0), mailer)
    for hour in (8, 12, 23):
        assert _run(dctx, local(2026, 10, 19, hour, 0), mailer).digests == []
    assert _run(dctx, local(2026, 10, 20, 7, 0), mailer).digests == []
    assert len(mailer.sent) == 1
    subject, body, files = mailer.sent[0]
    assert subject == "weekly 2026-10-12 to Rider cc " and body == "text"
    assert mailer.html == ["<p>html</p>"] and files[0].name == "weekly_2026-10-12.pdf"
    assert files[0].parent == dctx.reports_dir / "digest"
    row = _row(dctx, "weekly", "2026-10-12")
    assert (row.status, row.attempts) == ("sent", 1) and row.sent_at


def test_email_greets_the_profile_name_and_names_the_cc(data: Path, built: list[Any]) -> None:
    ctx = make_ctx(
        data,
        cyp_features=ON,
        cyp_report_email_name="Dad",
        cyp_report_email_cc="Roger <roger@example.test>, coach@example.test",
    )
    try:
        mailer = Mailer()
        _run(ctx, local(2026, 10, 19, 7, 0), mailer)
        assert mailer.sent[0][0] == "weekly 2026-10-12 to Dad cc Roger,coach@example.test"
    finally:
        ctx.close()


def test_monday_06_59_sends_nothing(dctx: AppContext, built: list[Any]) -> None:
    _anchor(dctx, "weekly", "2026-10-13")
    assert _run(dctx, local(2026, 10, 19, 6, 59), Mailer()).digests == []
    assert [t.period_start for t in _run(dctx, local(2026, 10, 19, 7, 0), Mailer()).digests] == [
        "2026-10-12"
    ]


def test_monthly_email_and_catch_up_after_sleep(dctx: AppContext, built: list[Any]) -> None:
    _anchor(dctx, "monthly", "2026-10-20")
    mailer = Mailer()
    # Asleep through 11-01 07:00, woken on the 3rd.
    report = _run(dctx, local(2026, 11, 3, 8, 0), mailer, ("monthly",))
    assert [(t.kind, t.period_start) for t in report.digests] == [("monthly", "2026-10-01")]
    assert mailer.sent[0][0].startswith("monthly 2026-10-01 to Rider")
    assert built == [("monthly", d("2026-10-01"))]


def test_manual_send_counts_as_sent(dctx: AppContext, built: list[Any]) -> None:
    _anchor(dctx, "weekly", "2026-10-15")
    mailer = Mailer()
    digest.send(dctx, "weekly", d("2026-10-12"), mailer=mailer, now=local(2026, 10, 19, 6, 0))
    assert _run(dctx, local(2026, 10, 19, 7, 0), mailer).digests == []
    assert len(mailer.sent) == 1


def test_a_running_period_is_never_sent(dctx: AppContext, built: list[Any]) -> None:
    with pytest.raises(ConfigError, match="not ended"):
        digest.send(dctx, "weekly", d("2026-10-12"), mailer=Mailer(), now=local(2026, 10, 18, 22))
    assert built == []


# ---------------------------------------------------------------------------------- retries


def test_email_failure_retries_three_times_then_gives_up(
    dctx: AppContext, built: list[Any], notices: list[str]
) -> None:
    _anchor(dctx, "weekly", "2026-10-15")
    mailer = Mailer(fail=EmailError("smtp down"))
    for attempt, status in ((1, "failed"), (2, "failed"), (3, "gave_up")):
        report = _run(dctx, local(2026, 10, 19 + attempt - 1, 7, 0), mailer)
        assert [t.action for t in report.digests] == [status] and not report.ok
        row = _row(dctx, "weekly", "2026-10-12")
        assert (row.status, row.attempts) == (status, attempt)
        assert row.error and "smtp down" in row.error
    assert len(notices) == 1 and "2026-10-12" in notices[0]
    assert _run(dctx, local(2026, 10, 22, 7, 0), mailer).digests == []  # no fourth attempt
    with dctx.factory() as s:
        runs = s.scalars(select(JobRun).where(JobRun.job == "digest")).all()
    assert len(runs) == 4 and runs[0].counts == {"failed": 1}


def test_a_failed_build_recovers_on_the_next_run(
    dctx: AppContext, built: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _anchor(dctx, "weekly", "2026-10-15")
    real = digest.build_weekly

    def broken(*_a: Any, **_k: Any) -> Any:
        raise ValueError("no fitness rows")

    monkeypatch.setattr(digest, "build_weekly", broken)
    mailer = Mailer()
    assert [t.action for t in _run(dctx, local(2026, 10, 19, 7, 0), mailer).digests] == ["failed"]
    monkeypatch.setattr(digest, "build_weekly", real)
    assert [t.action for t in _run(dctx, local(2026, 10, 20, 7, 0), mailer).digests] == ["sent"]
    row = _row(dctx, "weekly", "2026-10-12")
    assert (row.status, row.attempts, row.error) == ("sent", 2, None)


# ------------------------------------------------------------------------------------ flags


def test_flags_off_is_a_quiet_skip(data: Path, built: list[Any]) -> None:
    ctx = _ctx(data, "notify.macos=off,notify.email=on")
    try:
        report = job.digest_profile(ctx.settings, now=local(2026, 10, 19, 7, 0), mailer=Mailer())
        assert (report.status, report.digests, report.ok) == ("skipped", [], True)
        with ctx.factory() as s:
            assert s.scalars(select(JobRun).where(JobRun.job == "digest")).all() == []
            assert s.scalars(select(DigestReport)).all() == []
            assert s.get(SchemaMeta, "digest.weekly.first_enabled") is None
        with pytest.raises(ConfigError, match=r"report\.digest_weekly"):
            digest.build(ctx, "weekly", d("2026-10-05"))
        assert built == []
    finally:
        ctx.close()


def test_only_the_enabled_kind_runs(data: Path, built: list[Any]) -> None:
    ctx = _ctx(data, "notify.macos=off,report.digest_monthly=on,notify.email=on")
    try:
        report = job.digest_profile(ctx.settings, now=local(2026, 11, 1, 7, 0), mailer=Mailer())
        assert [t.kind for t in report.digests] == ["monthly"] and report.ok
    finally:
        ctx.close()


def test_email_off_builds_the_pdf_only(data: Path, built: list[Any]) -> None:
    ctx = _ctx(data, "notify.macos=off,report.digest_weekly=on")
    try:
        mailer = Mailer()
        assert [t.action for t in _run(ctx, local(2026, 10, 19, 7, 0), mailer).digests] == ["built"]
        assert mailer.sent == [] and _row(ctx, "weekly", "2026-10-12").sent_at is None
        assert _run(ctx, local(2026, 10, 20, 7, 0), mailer).digests == []
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------- lock


def test_lock_held_skips_and_records_a_job_run(dctx: AppContext, built: list[Any]) -> None:
    mailer = Mailer()
    sleeps: list[float] = []
    with profile_lock(dctx.settings.cyp_data_dir):
        report = job.digest_profile(
            dctx.settings,
            now=local(2026, 10, 19, 7, 0),
            mailer=mailer,
            lock_wait_s=10,
            sleep=sleeps.append,
        )
    assert report.status == "skipped" and "lock" in report.detail and report.ok
    assert sleeps == [5.0, 5.0] and mailer.sent == [] and built == []
    with dctx.factory() as s:
        run = s.scalars(select(JobRun).where(JobRun.job == "digest")).one()
    assert (run.status, run.counts) == ("ok", {"skipped_locked": 1})


def test_digest_profile_runs_under_the_lock(dctx: AppContext, built: list[Any]) -> None:
    report = job.digest_profile(dctx.settings, now=local(2027, 2, 1, 7, 0), mailer=Mailer())
    assert report.ok
    assert [(t["kind"], t["action"]) for t in report.to_dict()["digests"]] == [
        ("weekly", "sent"),
        ("monthly", "sent"),
    ]


# ------------------------------------------------------------------- fan-out, agent, CLI


def test_digest_all_runs_one_child_per_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    argv: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        argv.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"profile": "x", "ok": true}\n', "")

    monkeypatch.setattr("cyp.jobs.runner.subprocess.run", fake_run)
    results = job.digest_all(["a", "b"])
    assert [c[4:] for c in argv] == [
        ["a", "digest", "run", "--json"],
        ["b", "digest", "run", "--json"],
    ]
    assert all(r.ok for r in results)


def test_digest_agent_plist(tmp_path: Path) -> None:
    spec = digest_spec(tmp_path)
    plist = plistlib.loads(build_plist(AgentSpec(**{**spec.__dict__, "python": "/v/python"})))
    assert plist["Label"] == DIGEST_LABEL == "com.cy-performance.digest"
    assert plist["ProgramArguments"] == ["/v/python", "-m", "cyp.cli", "digest", "run", "--all"]
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 0}
    assert "StartInterval" not in plist and spec.log_path.name == "digest.log"


def test_cli_digest_run_with_the_flags_off_is_a_quiet_skip() -> None:
    res = CliRunner().invoke(app, ["digest", "run", "--json"])
    assert res.exit_code == 0, res.output
    assert '"status": "skipped"' in res.output and "report.digest_weekly" in res.output


def test_cli_digest_needs_the_flag(settings: Settings) -> None:
    from cyp.store.migrate import upgrade_head

    upgrade_head(settings.cyp_db_url)
    res = CliRunner().invoke(app, ["digest", "weekly", "--period", "2026-10-05"])
    assert res.exit_code == 1 and "report.digest_weekly is off" in res.output
    res = CliRunner().invoke(app, ["digest", "monthly", "--period", "2026-09"])
    assert res.exit_code == 1 and "report.digest_monthly is off" in res.output


def test_cli_send_needs_notify_email(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, built: list[Any]
) -> None:
    from cyp.store.migrate import upgrade_head

    upgrade_head(settings.cyp_db_url)
    monkeypatch.setenv("CYP_FEATURES", "report.digest_weekly=on")
    res = CliRunner().invoke(app, ["digest", "weekly", "--period", "2026-10-05", "--send"])
    assert res.exit_code == 2 and "notify.email is off" in res.output
    assert built == []


def test_cli_rejects_bad_periods(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from cyp.store.migrate import upgrade_head

    upgrade_head(settings.cyp_db_url)
    monkeypatch.setenv("CYP_FEATURES", "report.digest_weekly=on")
    res = CliRunner().invoke(app, ["digest", "weekly", "--period", "2026-10"])
    assert res.exit_code == 2 and "YYYY-MM-DD" in res.output
    res = CliRunner().invoke(app, ["digest", "weekly", "--period", "2026-10-06"])
    assert res.exit_code == 1 and "Monday" in res.output
