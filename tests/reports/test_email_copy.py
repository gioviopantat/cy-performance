"""Report email copy: addressed by name, who it is for, verdict-specific, deterministic."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cyp.reports.email_copy import compose_ride
from cyp.reports.ride_model import RideReport

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ride_report.full.json"


def _report() -> RideReport:
    return RideReport.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_ride_email_greets_names_and_says_who_it_is_for() -> None:
    r = _report()
    day = r.ride.start_local.date()
    c = compose_ride(r, to_name="William", cc_names=["Roger"], today=day)
    assert c.subject.startswith("【騎完報告】") and r.ride.name in c.subject
    assert c.text.startswith("William 你好，")
    assert "這封信寄給 William（副本：Roger）" in c.text
    assert "今天" in c.text  # ridden today
    assert "cy-performance 訓練團隊" in c.text
    assert "<!doctype html>" in c.html and "William 你好" in c.html


def test_same_ride_same_email() -> None:
    r = _report()
    day = r.ride.start_local.date()
    a = compose_ride(r, to_name="Roger", today=day)
    b = compose_ride(r, to_name="Roger", today=day)
    assert a == b


def test_overdone_easy_day_gets_the_overdone_copy() -> None:
    r = _report()
    assert r.intent is not None
    intent = r.intent.model_copy(
        update={
            "kind": "ceiling",
            "verdict": "missed",
            "load_ratio": 3.94,
            "planned_name": "恢復騎 45 分",
        }
    )
    r = r.model_copy(update={"intent": intent})
    c = compose_ride(r, to_name="Roger", today=r.ride.start_local.date() + dt.timedelta(days=1))
    assert "說好的「恢復騎 45 分」" in c.subject and "😅" in c.subject
    assert "394%" in c.text and "未達成（騎過頭）" in c.text
    assert "今天" not in c.text.splitlines()[2]  # an older ride is dated, not "today"


def test_unrated_ride_asks_for_a_rating_with_the_link() -> None:
    r = _report()
    feel = r.feel.model_copy(
        update={"rated": False, "rate_url": "https://intervals.icu/activities/i1"}
    )
    r = r.model_copy(update={"feel": feel})
    c = compose_ride(r, to_name="Roger", today=r.ride.start_local.date())
    assert "還沒評分" in c.text and "https://intervals.icu/activities/i1" in c.text
    assert "href='https://intervals.icu/activities/i1'" in c.html


def test_html_escapes_names() -> None:
    r = _report()
    r = r.model_copy(update={"ride": r.ride.model_copy(update={"name": "<b>x</b>"})})
    c = compose_ride(r, to_name="A&B", today=r.ride.start_local.date())
    assert "<b>x</b>" not in c.html and "A&amp;B" in c.html
