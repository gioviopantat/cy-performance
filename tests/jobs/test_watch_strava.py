"""Automatic Strava RIDE.LOG after a ride report (flag strava.auto_description, ADR-0020)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest

from cyp.jobs import watch
from cyp.services import ride_report
from cyp.services.context import AppContext
from cyp.store.models import Activity
from tests.jobs.test_watch import T0, Mailer, _ctx, _one_ride, _row, built, notices
from tests.services.test_ride_story import seed_spot
from tests.services.test_strava_write import FakeStrava, _token

__all__ = ["built", "notices"]  # pytest fixtures used below

WRITE = "notify.macos=off,report.ride=on,notify.email=on,sync.strava=on,strava.write_description=on"
AUTO = f"{WRITE},strava.auto_description=on,strava.route_story=on"
OWNER = 42


@pytest.fixture
def actx(data: Path) -> Iterator[AppContext]:
    c = _ctx(data, AUTO)
    yield c
    c.close()


def _strava_ride(ctx: AppContext, owner: int = OWNER) -> int:
    aid = _one_ride(ctx, rpe=5)
    with ctx.factory() as s:
        a = s.get(Activity, aid)
        assert a is not None
        a.strava_id = 777
        a.raw_strava_json = {"athlete": {"id": owner}}
        s.commit()
    seed_spot(ctx, aid)
    return aid


def _tick(ctx: AppContext, at: dt.datetime, mailer: Mailer, fake: FakeStrava) -> watch.WatchReport:
    return watch.tick(ctx, now=at, mailer=mailer, sync=lambda _c: {}, strava_client=lambda: fake)


def _actions(report: watch.WatchReport, aid: int) -> list[str]:
    return [r.action for r in report.rides if r.activity_id == aid]


def test_flag_off_never_touches_strava(data: Path, built: list[int]) -> None:
    ctx = _ctx(data, WRITE)  # write_description on, auto off
    try:
        _token(ctx, "read,activity:write", athlete=OWNER)
        aid = _strava_ride(ctx)
        fake, mailer = FakeStrava("my notes"), Mailer()
        report = _tick(ctx, T0, mailer, fake)
        assert _actions(report, aid) == ["sent"] and len(mailer.sent) == 1
        assert fake.writes == [] and _row(ctx, aid).strava_status is None
    finally:
        ctx.close()


def test_auto_flag_needs_write_description(data: Path) -> None:
    ctx = _ctx(data, "notify.macos=off,report.ride=on,strava.auto_description=on")
    try:
        assert not ctx.features().enabled("strava.auto_description")
    finally:
        ctx.close()


def test_writes_after_the_email_once_and_keeps_the_athletes_text(
    actx: AppContext, built: list[int]
) -> None:
    _token(actx, "read,activity:read_all,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    fake, mailer = FakeStrava("Coffee ride with Sherry"), Mailer()
    report = _tick(actx, T0, mailer, fake)
    assert _actions(report, aid) == ["sent", "strava_written"] and report.ok
    assert len(mailer.sent) == 1 and len(fake.writes) == 1
    desc = fake.writes[0][1]
    assert desc.startswith("Coffee ride with Sherry\n\n⚙️ RIDE.LOG")
    assert desc.endswith("#cyperformance")
    assert desc.count("⚙️ RIDE.LOG") == 1
    row = _row(actx, aid)
    assert (row.status, row.strava_status, row.strava_error) == ("sent", "written", None)
    # The next tick neither resends nor rewrites.
    again = _tick(actx, T0 + dt.timedelta(minutes=10), mailer, fake)
    assert _actions(again, aid) == [] and len(fake.writes) == 1 and len(mailer.sent) == 1


def test_idempotent_push_writes_nothing_when_the_block_is_there(
    actx: AppContext, built: list[int]
) -> None:
    from cyp.services.strava_write import auto_push

    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    fake = FakeStrava("notes")
    first = auto_push(actx, aid, client_factory=lambda: fake)
    second = auto_push(actx, aid, client_factory=lambda: fake)
    assert first.written and not second.written and len(fake.writes) == 1
    assert fake.description.count("⚙️ RIDE.LOG") == 1


def test_missing_scope_fails_then_gives_up_without_touching_the_email(
    actx: AppContext, built: list[int], notices: list[str]
) -> None:
    _token(actx, "read,activity:read_all", athlete=OWNER)  # no activity:write
    aid = _strava_ride(actx)
    fake, mailer = FakeStrava(""), Mailer()
    first = _tick(actx, T0, mailer, fake)
    assert _actions(first, aid) == ["sent", "strava_failed"] and first.ok
    row = _row(actx, aid)
    assert row.status == "sent" and row.strava_status == "failed"
    assert row.strava_error is not None and "activity:write" in row.strava_error
    second = _tick(actx, T0 + dt.timedelta(minutes=10), mailer, fake)
    assert _actions(second, aid) == ["strava_gave_up"]
    assert _row(actx, aid).strava_status == "gave_up" and len(notices) == 1
    third = _tick(actx, T0 + dt.timedelta(minutes=20), mailer, fake)
    assert _actions(third, aid) == []  # given up: no more tries
    assert fake.writes == [] and len(mailer.sent) == 1 and _row(actx, aid).status == "sent"


def test_owner_mismatch_is_refused(actx: AppContext, built: list[int]) -> None:
    _token(actx, "read,activity:write", athlete=7)  # another athlete's token
    aid = _strava_ride(actx)
    fake = FakeStrava("", owner=OWNER)
    report = _tick(actx, T0, Mailer(), fake)
    assert _actions(report, aid) == ["sent", "strava_failed"]
    err = _row(actx, aid).strava_error
    assert err is not None and "refusing to write" in err and fake.writes == []


def test_a_strava_crash_is_retried_once_and_recovers(actx: AppContext, built: list[int]) -> None:
    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)

    class Flaky(FakeStrava):
        calls = 0

        def update_activity_description(
            self, activity_id: int, description: str
        ) -> dict[str, object]:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise ConnectionError("strava down")
            return super().update_activity_description(activity_id, description)

    fake, mailer = Flaky(""), Mailer()
    assert _actions(_tick(actx, T0, mailer, fake), aid) == ["sent", "strava_failed"]
    assert _row(actx, aid).status == "sent" and len(mailer.sent) == 1
    retry = _tick(actx, T0 + dt.timedelta(minutes=10), mailer, fake)
    assert _actions(retry, aid) == ["strava_written"] and len(fake.writes) == 1
    assert _row(actx, aid).strava_status == "written" and _row(actx, aid).strava_error is None


def test_failed_email_writes_no_strava(actx: AppContext, built: list[int]) -> None:
    from cyp.jobs.notify import EmailError

    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    fake = FakeStrava("")
    report = _tick(actx, T0, Mailer(fail=EmailError("smtp down")), fake)
    assert _actions(report, aid) == ["failed"] and fake.writes == []
    assert _row(actx, aid).strava_status is None
    assert ride_report.strava_retries(actx) == []


def test_story_is_prepared_before_the_write_and_never_blocks_it(
    actx: AppContext, built: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyp.services import ride_story

    order: list[str] = []
    real = watch.strava_write.auto_push
    monkeypatch.setattr(ride_story, "prepare", lambda c, aid, **_k: order.append("prepare"))

    def push(ctx: AppContext, aid: int, **kw: object) -> object:
        order.append("push")
        return real(ctx, aid, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(watch.strava_write, "auto_push", push)
    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    report = _tick(actx, T0, Mailer(), FakeStrava(""))
    assert order == ["prepare", "push"] and _actions(report, aid) == ["sent", "strava_written"]


def test_a_ride_with_nothing_concrete_leaves_strava_alone(
    actx: AppContext, built: list[int]
) -> None:
    """No RIDE.LOG (nothing concrete to suggest) is not a failure: no write, no notice."""
    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    (actx.settings.cyp_data_dir / "ride_story" / "osm" / f"{aid}.json").unlink()
    actx.settings = actx.settings.model_copy(update={"cyp_features": AUTO.replace(
        ",strava.route_story=on", "")})  # fmt: skip
    fake = FakeStrava("my notes")
    report = _tick(actx, T0, Mailer(), fake)
    assert _actions(report, aid) == ["sent", "strava_unchanged"] and fake.writes == []
    assert _row(actx, aid).strava_status == "unchanged"


def test_a_failed_route_lookup_is_retried_and_strava_rewritten(
    actx: AppContext, built: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ride 232: OSM down at first; an hour later the spot is found and Strava gets it."""
    import os

    from cyp.ingest.osm.client import OsmError
    from cyp.services import ride_story
    from tests.services.test_ride_story import T as STORY

    class Down:
        def places_in(self, *_a: object, **_k: object) -> list[object]:
            raise OsmError("overpass: 500 Internal Server Error")

        def close(self) -> None:
            pass

    monkeypatch.setattr(ride_story, "OsmClient", lambda **_k: Down())

    _token(actx, "read,activity:write", athlete=OWNER)
    aid = _strava_ride(actx)
    osm = actx.settings.cyp_data_dir / "ride_story" / "osm" / f"{aid}.json"
    osm.unlink()  # no cache: the lookup goes out and fails (no network in tests)
    fake = FakeStrava("my notes")
    first = _tick(actx, T0, Mailer(), fake)
    assert _actions(first, aid) == ["sent", "strava_unchanged"] and fake.writes == []
    seed_spot(actx, aid)  # Overpass answers now
    story = actx.settings.cyp_data_dir / "ride_story" / f"{aid}.json"
    soon = _tick(actx, T0 + dt.timedelta(minutes=10), Mailer(), fake)
    assert _actions(soon, aid) == [] and fake.writes == []  # not before an hour
    old = (T0 - dt.timedelta(hours=2)).timestamp()
    os.utime(story, (old, old))
    later = _tick(actx, T0 + dt.timedelta(hours=1), Mailer(), fake)
    assert _actions(later, aid) == ["strava_written"] and len(fake.writes) == 1
    assert STORY["places"]["viewpoint"] in fake.writes[0][1]
    assert not (actx.settings.cyp_data_dir / "ride_logs" / f"{aid}.txt").exists()
