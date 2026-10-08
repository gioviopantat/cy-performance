"""external_id scheme, diff rules, publisher apply/verify/idempotency, M3 spike, client, CLI."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from cyp.cli import app
from cyp.core.ids import external_id, parse_external_id
from cyp.ingest.intervals.auth import ApiKeyAuth
from cyp.ingest.intervals.client import BASE_URL, IntervalsClient
from cyp.publish.diff import compute_diff, mutable
from cyp.publish.events import EventSpec, is_ours
from cyp.publish.publisher import Publisher, verify_load
from cyp.publish.spike import SPIKE_NAME, plan_spike, run_spike
from cyp.store.models import PublishLog
from tests.publish.conftest import FakeCalendar, day

NOW = dt.datetime(2026, 10, 2, 8, 0)  # local, before the 10:00 cut-off
WINDOW = (day(0), day(13))


def spec(
    offset: int, *, slot: int = 1, minutes: int = 60, name: str = "Z2 60", load: float | None = None
) -> EventSpec:
    d = day(offset)
    return EventSpec(
        external_id=external_id("s2026a", d, slot),
        date=d,
        name=name,
        description="- 60m 65%",
        moving_time_s=minutes * 60,
        target_load=load if load is not None else float(minutes),
        tags=["endurance"],
    )


# ----------------------------------------------------------------------------- events


def test_external_id_roundtrip_and_ownership() -> None:
    ext = external_id("s2026a", dt.date(2026, 10, 6), 2)
    assert ext == "cyp:s2026a:2026-10-06:2"
    assert parse_external_id(ext) == ("s2026a", dt.date(2026, 10, 6), 2)
    assert parse_external_id("strava:123") is None and parse_external_id(None) is None
    assert is_ours({"external_id": ext}) and not is_ours({"external_id": "x"})
    assert not is_ours({"name": "athlete race"})
    with pytest.raises(ValueError):
        external_id("bad:key", dt.date(2026, 10, 6))


def test_payload_shape() -> None:
    p = spec(4).payload()
    assert p["category"] == "WORKOUT" and p["type"] == "Ride" and p["target"] == "POWER"
    assert p["start_date_local"] == "2026-10-06T00:00:00"
    assert p["tags"] == ["cyp", "endurance"] and p["moving_time"] == 3600
    assert "uid" not in p
    assert spec(4).payload(uid=True)["uid"] == p["external_id"]
    note = EventSpec(
        external_id="cyp:s:2026-10-06:9", date=dt.date(2026, 10, 6), name="n", category="NOTE"
    )
    assert "type" not in note.payload()


# ------------------------------------------------------------------------------- diff


def test_mutability_window() -> None:
    assert not mutable(day(-1), NOW, dt.time(10))
    assert mutable(day(0), NOW, dt.time(10))
    assert not mutable(day(0), NOW.replace(hour=11), dt.time(10))
    assert mutable(day(1), NOW.replace(hour=23), dt.time(10))


def test_diff_buckets() -> None:
    cal = FakeCalendar()
    same = spec(1)
    cal.add(**same.payload())  # identical -> noop
    cal.add(**spec(2, name="old name").payload())  # changed -> update
    cal.add(**spec(5).payload())  # no longer wanted -> delete
    cal.add(name="Gran Fondo", category="RACE_A", start_date_local="2026-10-05T00:00:00")
    cal.add(**spec(-1).payload())  # past: frozen
    desired = [same, spec(2), spec(3), spec(-1, name="changed past")]
    d = compute_diff(
        desired, cal.list_events(day(-3), day(20)), window=(day(-3), day(13)), now_local=NOW
    )
    assert [s.external_id for s in d.create] == [spec(3).external_id]
    assert [s.external_id for s, _ in d.update] == [spec(2).external_id]
    assert [e["external_id"] for e in d.delete] == [spec(5).external_id]
    assert [s.external_id for s in d.noop] == [same.external_id]
    assert d.frozen == [spec(-1).external_id]
    assert d.n_writes == 3
    # The athlete's own race is never touched.
    assert all(e.get("name") != "Gran Fondo" for e in d.delete)


def test_diff_declined_and_budget() -> None:
    desired = [spec(i) for i in range(1, 6)]
    d = compute_diff(
        desired,
        [],
        window=WINDOW,
        now_local=NOW,
        previously_published=[spec(1).external_id],
        max_events=2,
    )
    assert d.declined == [spec(1).external_id]
    assert len(d.create) == 2 and d.deferred == [spec(4).external_id, spec(5).external_id]


# -------------------------------------------------------------------------- publisher


def test_propose_and_apply_without_permission_never_write() -> None:
    cal = FakeCalendar()
    pub = Publisher(cal)
    r = pub.run([spec(1)], window=WINDOW, now_local=NOW)
    assert r.mode == "propose" and r.diff.summary()["create"] == 1 and cal.writes == []
    r2 = pub.run([spec(1)], window=WINDOW, now_local=NOW, mode="apply")
    assert r2.written == 0 and cal.writes == []


def test_apply_writes_verifies_and_is_idempotent(factory: sessionmaker[Session]) -> None:
    cal = FakeCalendar(load_per_min=1.0)
    cal.add(**spec(6).payload())  # stale event of ours
    pub = Publisher(cal, factory)
    desired = [spec(1), spec(2, minutes=90, load=60.0)]  # 2nd: icu computes 90 vs target 60
    r = pub.run(desired, window=WINDOW, now_local=NOW, mode="apply", allow_write=True, run_id=None)
    assert r.written == 2 and r.deleted == 1 and r.failed is None
    assert r.verified == [spec(1).external_id]
    assert r.needs_review == [spec(2).external_id]
    assert {e["external_id"] for e in cal.events} == {spec(1).external_id, spec(2).external_id}
    with factory() as s:
        rows = s.scalars(select(PublishLog).order_by(PublishLog.id)).all()
        assert [(r_.action, r_.status) for r_ in rows] == [
            ("create", "ok"),
            ("create", "needs_review"),
            ("delete", "ok"),
        ]
        assert rows[0].verified_load == 60.0 and rows[0].icu_event_id is not None
    n_events = len(cal.events)
    again = pub.run(desired, window=WINDOW, now_local=NOW, mode="apply", allow_write=True)
    assert again.diff.n_writes == 0 and len(again.diff.noop) == 2
    assert len(cal.events) == n_events


def test_uid_mode_payloads_carry_uid() -> None:
    cal = FakeCalendar(honours="uid")
    pub = Publisher(cal, upsert_mode="uid")
    pub.run([spec(1)], window=WINDOW, now_local=NOW, mode="apply", allow_write=True)
    pub.run([spec(1, name="renamed")], window=WINDOW, now_local=NOW, mode="apply", allow_write=True)
    assert len(cal.events) == 1 and cal.events[0]["name"] == "renamed"
    assert cal.writes[0][0] == "bulk:uid" and cal.writes[0][1][0]["uid"] == spec(1).external_id


def test_verify_load() -> None:
    assert verify_load(100, 109, 0.10) is True
    assert verify_load(100, 111, 0.10) is False
    assert verify_load(None, 50, 0.10) is None and verify_load(100, None, 0.10) is None


# ------------------------------------------------------------------------------ spike


@pytest.mark.parametrize(
    ("honours", "expected", "copies"),
    [
        ("external_id", "upsert", {"upsert": 1}),
        ("uid", "uid", {"upsert": 2, "uid": 1}),
        ("none", "none", {"upsert": 2, "uid": 2}),
    ],
)
def test_spike_detects_mode_and_cleans_up(honours: str, expected: str, copies: dict) -> None:
    cal = FakeCalendar(honours=honours)  # type: ignore[arg-type]
    target = dt.date.today() + dt.timedelta(days=400)
    cal.add(
        name="athlete's own event",
        category="NOTE",
        start_date_local=f"{target.isoformat()}T00:00:00",
    )
    result = run_spike(cal, target)
    assert result.supported == expected
    assert result.copies == copies
    assert result.leftovers == []
    assert [e["name"] for e in cal.events] == ["athlete's own event"]  # only ours removed
    assert all(e.get("name") != SPIKE_NAME for e in cal.events)


def test_spike_refuses_past_dates() -> None:
    with pytest.raises(ValueError):
        run_spike(FakeCalendar(), dt.date.today())
    assert len(plan_spike(dt.date(2027, 12, 31))) == 4


# ----------------------------------------------------------------------- client + CLI


def test_client_bulk_endpoints_send_mode_params() -> None:
    with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
        bulk = router.post("/athlete/i1/events/bulk").mock(
            return_value=httpx.Response(200, json=[{"id": 1}])
        )
        delete = router.put("/athlete/i1/events/bulk-delete").mock(
            return_value=httpx.Response(200, json={"eventsDeleted": 1})
        )
        client = IntervalsClient(auth=ApiKeyAuth("k"), athlete_id="i1", sleep=lambda _s: None)
        assert client.bulk_upsert_events([spec(1).payload()]) == [{"id": 1}]
        assert bulk.calls.last.request.url.params["upsert"] == "true"
        client.bulk_upsert_events([spec(1).payload(uid=True)], mode="uid")
        req = bulk.calls.last.request
        assert req.url.params["upsertOnUid"] == "true" and "upsert" not in req.url.params
        assert json.loads(req.content)[0]["uid"] == spec(1).external_id
        client.bulk_delete_events([{"id": 1}])
        assert json.loads(delete.calls.last.request.content) == [{"id": 1}]
        client.close()


def test_cli_spike_dry_run_and_guards(data_dir: Path, db_url: str) -> None:
    env = {"CYP_DATA_DIR": str(data_dir), "CYP_DB_URL": db_url}
    runner = CliRunner()
    future = (dt.date.today() + dt.timedelta(days=400)).isoformat()
    with respx.mock(assert_all_mocked=True) as router:  # any network call would fail the test
        res = runner.invoke(app, ["publish", "spike", "--date", future], env=env)
        assert res.exit_code == 0, res.output
        assert "dry run" in res.output and "--confirm-write" in res.output
        assert router.calls.call_count == 0
    past = runner.invoke(app, ["publish", "spike", "--date", "2020-01-01"], env=env)
    assert past.exit_code == 2
    nokey = runner.invoke(app, ["publish", "spike", "--date", future, "--confirm-write"], env=env)
    assert nokey.exit_code == 2 and "INTERVALS_API_KEY" in nokey.output
