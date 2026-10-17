"""Calendar view: compliance status per day and the endpoint (services/calendar.py)."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from cyp.services.calendar import status_of
from cyp.services.context import AppContext
from cyp.store.models import Athlete, IcuEvent

TODAY = dt.date(2026, 10, 6)
PAST = TODAY - dt.timedelta(days=1)
FUTURE = TODAY + dt.timedelta(days=1)


@pytest.mark.parametrize(
    ("day", "planned", "load", "expected"),
    [
        (PAST, 88.0, 90.0, "done"),
        (PAST, 88.0, 120.0, "over"),  # > 130 %
        (PAST, 88.0, 30.0, "under"),  # < 50 %
        (PAST, 88.0, 0.0, "skipped"),
        (PAST, None, 60.0, "extra"),
        (PAST, None, 0.0, "rest"),
        (TODAY, 88.0, 0.0, "pending"),  # the day is not over yet
        (TODAY, 88.0, 30.0, "pending"),
        (TODAY, 88.0, 90.0, "done"),
        (FUTURE, 88.0, 0.0, "planned"),
        (FUTURE, None, 0.0, "rest"),
    ],
)
def test_status_of(day: dt.date, planned: float | None, load: float, expected: str) -> None:
    assert status_of(day, TODAY, planned, load) == expected


def test_calendar_endpoint(client: TestClient, ctx: AppContext) -> None:
    with ctx.factory() as s:
        athlete = s.query(Athlete).first()
        assert athlete is not None
        s.add(
            IcuEvent(
                id=880001,
                category="WORKOUT",
                start_date_local=f"{PAST}T00:00:00",
                name="自己排的爬坡",
                icu_training_load=500.0,
            )
        )
        s.add(IcuEvent(id=880002, category="SICK", start_date_local=f"{FUTURE}T00:00:00"))
        s.commit()
    r = client.get("/v1/calendar", params={"start": "2026-09-30", "end": "2026-10-08"})
    assert r.status_code == 200
    body = r.json()
    assert body["start"] == "2026-09-28" and body["end"] == "2026-10-11"  # whole weeks
    assert len(body["days"]) == 14 and len(body["weeks"]) == 2
    days = {d["date"]: d for d in body["days"]}
    past = days[PAST.isoformat()]
    assert past["planned"] == {
        "name_zh": "自己排的爬坡",
        "source": "calendar",
        "tss": 500.0,
        "minutes": None,
        "role": None,
        "outdoor": None,
    }
    assert past["status"] in ("under", "skipped")  # the athlete's own 500-TSS plan wins
    assert days[FUTURE.isoformat()]["notes"] == ["生病"]
    week = body["weeks"][1]
    assert week["start"] == "2026-10-05" and week["load_planned"] >= 500.0

    too_long = client.get("/v1/calendar", params={"start": "2026-08-01", "end": "2026-10-08"})
    assert too_long.status_code == 422
    backwards = client.get("/v1/calendar", params={"start": "2026-10-08", "end": "2026-10-01"})
    assert backwards.status_code == 422
