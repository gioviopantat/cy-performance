"""Ride watcher: timing rule, dedupe, retries, lock, flags (docs/specs/ride-report.md)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from cyp.core.errors import ConfigError
from cyp.jobs import watch
from cyp.jobs.notify import EmailError
from cyp.jobs.runner import profile_lock
from cyp.jobs.watch import spawn_tick as real_spawn_tick  # conftest stubs the attribute
from cyp.services import ride_report
from cyp.services.context import AppContext
from cyp.settings import Settings
from cyp.store.models import Activity, JobRun, RideReport
from tests.api.conftest import make_ctx

UTC = dt.UTC
#: 2026-10-06 07:00 in Asia/Taipei, the seeded store's "now" (rides up to 2026-10-05).
T0 = dt.datetime(2026, 10, 5, 23, 0, tzinfo=UTC)
ON = "notify.macos=off,report.ride=on,notify.email=on"


class Mailer:
    """Records sends; raises ``fail`` when set."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, list[Path]]] = []
        self.fail = fail

    def __call__(
        self, settings: Settings, subject: str, body: str, attachments: Sequence[Path]
    ) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((subject, list(attachments)))


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Fake the three contract stubs (other sessions own them); records built activity ids."""
    calls: list[int] = []

    def fake_build(session: Any, store: Any, activity_id: int, **_kw: Any) -> int:
        calls.append(activity_id)
        return activity_id

    def fake_render(report: int, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{report}.pdf"
        path.write_bytes(b"%PDF-1.7 fake")
        return path

    monkeypatch.setattr(ride_report, "build_ride_report", fake_build)
    monkeypatch.setattr(ride_report, "render_ride_pdf", fake_render)
    monkeypatch.setattr(ride_report, "tomorrow_preview", lambda ctx, day: None)
    monkeypatch.setattr(watch.trends_service, "recompute", lambda *_a, **_k: {})
    return calls


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    out: list[str] = []
    monkeypatch.setattr(watch, "notify_failure", lambda _s, msg: out.append(msg))
    return out


def _ctx(data: Path, features: str = ON) -> AppContext:
    return make_ctx(data, cyp_features=features, intervals_api_key="test-key")


@pytest.fixture
def wctx(data: Path) -> Iterator[AppContext]:
    c = _ctx(data)
    yield c
    c.close()


def _one_ride(ctx: AppContext, *, rpe: int | None = None) -> int:
    """Keep only the newest ride in the 2-day window; clear its rating."""
    with ctx.factory() as s:
        rides = s.scalars(
            select(Activity).where(Activity.is_ride.is_(True)).order_by(Activity.start_local.desc())
        ).all()
        keep = rides[0]
        for a in rides[1:]:
            if (a.start_local or "") >= "2026-10-05":
                a.is_ride = False
        keep.icu_rpe, keep.feel = rpe, None
        s.commit()
        assert (keep.start_local or "") >= "2026-10-05"
        return keep.id


def _tick(ctx: AppContext, at: dt.datetime, mailer: Mailer) -> watch.WatchReport:
    return watch.tick(ctx, now=at, mailer=mailer, sync=lambda _c: {})


def _row(ctx: AppContext, aid: int) -> RideReport:
    with ctx.factory() as s:
        row = s.get(RideReport, aid)
        assert row is not None
        s.expunge(row)
        return row


# ------------------------------------------------------------------------------ timing rule


@pytest.mark.parametrize(
    ("age_h", "streams", "rated", "want"),
    [
        (0, True, True, "send"),
        (0, True, False, "wait_feel"),
        (1.99, True, False, "wait_feel"),
        (2, True, False, "send"),
        (0, False, True, "wait_streams"),
        (5.99, False, False, "wait_streams"),
        (6, False, False, "send"),
    ],
)
def test_decide(age_h: float, streams: bool, rated: bool, want: str) -> None:
    now = T0 + dt.timedelta(hours=age_h)
    assert watch.decide(T0, now, has_streams=streams, rated=rated) == want


def test_unrated_ride_waits_two_hours_then_is_sent_once(wctx: AppContext, built: list[int]) -> None:
    aid = _one_ride(wctx)
    mailer = Mailer()
    first = _tick(wctx, T0, mailer)
    assert [(r.activity_id, r.action) for r in first.rides] == [(aid, "wait_feel")]
    assert _row(wctx, aid).status == ride_report.PENDING and not mailer.sent
    assert _tick(wctx, T0 + dt.timedelta(minutes=110), mailer).rides[0].action == "wait_feel"

    sent = _tick(wctx, T0 + dt.timedelta(hours=2), mailer)
    assert [(r.activity_id, r.action) for r in sent.rides] == [(aid, "sent")]
    assert len(mailer.sent) == 1 and mailer.sent[0][1][0].name == f"{aid}.pdf"
    row = _row(wctx, aid)
    assert (row.status, row.attempts, row.sent_at) == ("sent", 1, "2026-10-06T01:00:00Z")
    assert row.first_seen_at == "2026-10-05T23:00:00Z"

    again = _tick(wctx, T0 + dt.timedelta(hours=3), mailer)  # dedupe
    assert again.rides == [] and len(mailer.sent) == 1 and built == [aid]


def test_rated_ride_is_sent_on_the_first_tick(wctx: AppContext, built: list[int]) -> None:
    _one_ride(wctx, rpe=6)
    mailer = Mailer()
    assert _tick(wctx, T0, mailer).rides[0].action == "sent"
    assert len(mailer.sent) == 1


def test_web_rating_ends_the_wait(wctx: AppContext, built: list[int]) -> None:
    from cyp.services import ride_feedback

    aid = _one_ride(wctx)
    mailer = Mailer()
    assert _tick(wctx, T0, mailer).rides[0].action == "wait_feel"
    ride_feedback.save(wctx, aid, 7, None)  # spawns nothing here: the tick is driven below
    assert _tick(wctx, T0 + dt.timedelta(minutes=10), mailer).rides[0].action == "sent"


def test_missing_streams_wait_six_hours_then_send_without(
    wctx: AppContext, built: list[int]
) -> None:
    aid = _one_ride(wctx, rpe=5)
    wctx.store.delete(aid)
    with wctx.factory() as s:
        a = s.get(Activity, aid)
        assert a is not None
        a.pending_streams = True
        s.commit()
    mailer = Mailer()
    assert _tick(wctx, T0, mailer).rides[0].action == "wait_streams"
    assert _tick(wctx, T0 + dt.timedelta(hours=5), mailer).rides[0].action == "wait_streams"
    late = _tick(wctx, T0 + dt.timedelta(hours=6), mailer)
    assert (late.rides[0].action, late.rides[0].detail) == ("sent", "sent without streams")
    assert len(mailer.sent) == 1


def test_email_failure_retries_three_times_then_gives_up(
    wctx: AppContext, built: list[int], notices: list[str]
) -> None:
    aid = _one_ride(wctx, rpe=5)
    mailer = Mailer(fail=EmailError("smtp down"))
    for attempt, status in ((1, "failed"), (2, "failed"), (3, "gave_up")):
        report = _tick(wctx, T0 + dt.timedelta(minutes=10 * attempt), mailer)
        assert report.rides[0].action == status and not report.ok
        row = _row(wctx, aid)
        assert (row.status, row.attempts) == (status, attempt)
        assert row.error and "smtp down" in row.error
    assert len(notices) == 1 and str(aid) in notices[0]
    assert _tick(wctx, T0 + dt.timedelta(hours=1), mailer).rides == []  # no fourth attempt
    with wctx.factory() as s:
        runs = s.scalars(select(JobRun).where(JobRun.job == "watch")).all()
    assert len(runs) == 4 and runs[0].counts == {"failed": 1}


def test_a_failed_send_recovers_on_the_next_tick(wctx: AppContext, built: list[int]) -> None:
    aid = _one_ride(wctx, rpe=5)
    mailer = Mailer(fail=EmailError("timeout"))
    assert _tick(wctx, T0, mailer).rides[0].action == "failed"
    mailer.fail = None
    assert _tick(wctx, T0 + dt.timedelta(minutes=10), mailer).rides[0].action == "sent"
    row = _row(wctx, aid)
    assert (row.status, row.attempts, row.error) == ("sent", 2, None)


def test_sync_failure_fails_the_row_but_still_sends(wctx: AppContext, built: list[int]) -> None:
    from cyp.core.errors import IngestError

    _one_ride(wctx, rpe=5)
    mailer = Mailer()

    def broken(_ctx: AppContext) -> dict[str, Any]:
        raise IngestError("icu 500")

    report = watch.tick(wctx, now=T0, mailer=mailer, sync=broken)
    assert report.status == "failed" and "icu 500" in report.detail
    assert report.rides[0].action == "sent" and len(mailer.sent) == 1
    with wctx.factory() as s:
        run = s.scalars(select(JobRun).where(JobRun.job == "watch")).one()
    assert run.status == "failed"


# ------------------------------------------------------------------------------------ flags


def test_flag_off_builds_nothing_and_leaves_no_row(data: Path, built: list[int]) -> None:
    ctx = _ctx(data, "notify.macos=off")
    try:
        _one_ride(ctx, rpe=5)
        report = watch.watch_profile(ctx.settings, now=T0, mailer=Mailer())
        assert (report.status, report.rides) == ("skipped", [])
        with ctx.factory() as s:
            assert s.scalars(select(JobRun).where(JobRun.job == "watch")).all() == []
            assert s.scalars(select(RideReport)).all() == []
        with pytest.raises(ConfigError, match=r"report\.ride"):
            ride_report.build(ctx, 1)
        assert built == []
    finally:
        ctx.close()


def test_email_off_builds_the_pdf_only(data: Path, built: list[int]) -> None:
    ctx = _ctx(data, "notify.macos=off,report.ride=on")
    try:
        aid = _one_ride(ctx, rpe=5)
        mailer = Mailer()
        assert _tick(ctx, T0, mailer).rides[0].action == "built"
        assert mailer.sent == [] and _row(ctx, aid).sent_at is None
        assert _tick(ctx, T0 + dt.timedelta(hours=1), mailer).rides == []
    finally:
        ctx.close()


# ------------------------------------------------------------------------------------- lock


def test_lock_held_skips_and_records_a_job_run(wctx: AppContext, built: list[int]) -> None:
    _one_ride(wctx, rpe=5)
    mailer = Mailer()
    with profile_lock(wctx.settings.cyp_data_dir):
        report = watch.watch_profile(wctx.settings, now=T0, mailer=mailer, sync=lambda _c: {})
    assert report.status == "skipped" and "lock" in report.detail and report.ok
    assert mailer.sent == [] and built == []
    with wctx.factory() as s:
        run = s.scalars(select(JobRun).where(JobRun.job == "watch")).one()
    assert (run.status, run.counts) == ("ok", {"skipped_locked": 1})


def test_watch_profile_runs_a_tick_under_the_lock(wctx: AppContext, built: list[int]) -> None:
    aid = _one_ride(wctx, rpe=5)
    mailer = Mailer()
    report = watch.watch_profile(wctx.settings, now=T0, mailer=mailer, sync=lambda _c: {})
    assert report.ok and [r.activity_id for r in report.rides] == [aid]
    assert report.to_dict()["rides"][0]["action"] == "sent"


def test_profile_lock_waits_then_gives_up(tmp_path: Path) -> None:
    from cyp.jobs.runner import RunLockedError

    sleeps: list[float] = []
    with (
        profile_lock(tmp_path),
        pytest.raises(RunLockedError),
        profile_lock(tmp_path, wait_s=12, sleep=sleeps.append),
    ):
        pass
    assert sleeps == [5.0, 5.0, 5.0]


# ---------------------------------------------------------------------------- fan-out, spawn


def test_watch_all_runs_one_watch_child_per_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    argv: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        argv.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"profile": "x", "ok": true}\n', "")

    monkeypatch.setattr("cyp.jobs.runner.subprocess.run", fake_run)
    results = watch.watch_all(["a", "b"])
    assert [c[4:] for c in argv] == [["a", "watch", "--json"], ["b", "watch", "--json"]]
    assert all(r.ok for r in results)


def test_spawn_tick_does_not_wait(monkeypatch: pytest.MonkeyPatch, wctx: AppContext) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kw: Any) -> None:
        seen.update(cmd=cmd, **kw)

    monkeypatch.setattr(watch.subprocess, "Popen", fake_popen)
    real_spawn_tick(wctx.settings.model_copy(update={"cyp_profile": "roger"}))
    assert seen["cmd"][-3:] == ["--profile", "roger", "watch"]
    assert seen["start_new_session"] is True


@pytest.mark.parametrize("features", [ON, "notify.macos=off"])
def test_rating_save_starts_a_tick_only_with_report_ride(
    monkeypatch: pytest.MonkeyPatch, data: Path, features: str
) -> None:
    from cyp.services import ride_feedback

    started: list[str] = []
    monkeypatch.setattr(watch, "spawn_tick", lambda s: started.append(s.cyp_profile))
    ctx = _ctx(data, features)
    try:
        aid = _one_ride(ctx)
        ride_feedback.save(ctx, aid, 7, 2)
        assert len(started) == (1 if features == ON else 0)
    finally:
        ctx.close()


def test_rating_save_survives_a_failed_spawn(
    monkeypatch: pytest.MonkeyPatch, wctx: AppContext
) -> None:
    from cyp.services import ride_feedback

    def broken(_s: Settings) -> None:
        raise OSError("fork failed")

    monkeypatch.setattr(watch, "spawn_tick", broken)
    aid = _one_ride(wctx)
    assert ride_feedback.save(wctx, aid, 7, None).rpe == 7


def test_tomorrow_preview_only_for_recent_rides(
    wctx: AppContext, built: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resend or backfill of an old ride gets no section 8: that next day already happened."""
    days: list[dt.date] = []
    monkeypatch.setattr(ride_report, "tomorrow_preview", lambda _c, day: days.append(day))
    with wctx.factory() as s:
        rides = s.scalars(select(Activity).where(Activity.is_ride.is_(True))).all()
        newest = max(rides, key=lambda a: a.start_local or "")
        oldest = min(rides, key=lambda a: a.start_local or "")
        newest_id, oldest_id = newest.id, oldest.id
    monkeypatch.setattr(wctx, "fixed_now", None)
    ride_day = dt.date.fromisoformat((newest.start_local or "")[:10])
    monkeypatch.setattr(type(wctx), "today", lambda _self: ride_day)
    ride_report.build(wctx, oldest_id)
    assert days == []
    ride_report.build(wctx, newest_id)
    assert days == [ride_day + dt.timedelta(days=1)]
