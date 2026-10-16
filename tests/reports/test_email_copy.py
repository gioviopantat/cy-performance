"""Report email copy: addressed by name, who it is for, verdict-specific, deterministic."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cyp.reports.digest_model import (
    Consistency,
    DigestHeadline,
    Load,
    MonthlyDigest,
    NextPlan,
    Period,
    PlannedSession,
    SessionLine,
    WeeklyDigest,
)
from cyp.reports.email_copy import compose_monthly, compose_ride, compose_weekly
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


def _period(is_break: bool = False) -> Period:
    return Period(
        start=dt.date(2026, 10, 5),
        end=dt.date(2026, 10, 11),
        label_zh="10/5–10/11",
        is_break=is_break,
        break_reason_zh="10/16–10/25 出國旅行，沒有騎車" if is_break else "",
    )


def _weekly(is_break: bool = False) -> WeeklyDigest:
    return WeeklyDigest(
        profile="t",
        athlete_name="Test Rider",
        generated_at=dt.datetime(2026, 10, 12, 0, 0, tzinfo=dt.UTC),
        period=_period(is_break),
        headline=DigestHeadline(
            text_zh="4 堂課完成 3 堂，負荷 98 %。",
            verdict="break" if is_break else "on_track",
            change_zh="" if is_break else "週六長騎前一天早點睡",
        ),
        sessions=[
            SessionLine(day=dt.date(2026, 10, 6), planned_name="甜蜜點 3x10", verdict="achieved"),
            SessionLine(day=dt.date(2026, 10, 8), planned_name="恢復騎 45 分", verdict="overdone"),
        ],
        load=Load(planned_tss=400, actual_tss=392, hours=7.5, rides=4, ctl_start=50, ctl_end=52.5),
        next_week=NextPlan(
            sessions=[
                PlannedSession(day=dt.date(2026, 10, 13), name="Z2 90 分"),
                PlannedSession(day=dt.date(2026, 10, 14), name="甜蜜點金字塔"),
            ]
        ),
    )


def test_weekly_email() -> None:
    c = compose_weekly(
        _weekly(), to_name="William", cc_names=["Roger"], today=dt.date(2026, 10, 12)
    )
    assert c.subject.startswith("【週報】10/5–10/11 William") and "4 趟" in c.subject
    assert "William 你好" in c.text and "課表完成度：1/2 堂達成（其中 1 堂騎過頭）" in c.text
    assert "下週只改一件事：週六長騎前一天早點睡" in c.text
    assert "10/13（週二）：Z2 90 分" in c.text
    assert "這封信寄給 William（副本：Roger）" in c.text


def test_break_week_email_is_short_and_says_why() -> None:
    c = compose_weekly(_weekly(is_break=True), to_name="William", today=dt.date(2026, 10, 19))
    assert "休息週" in c.subject and "出國旅行" in c.text
    assert "這週三個重點" not in c.text


def test_monthly_email() -> None:
    m = MonthlyDigest(
        profile="t",
        athlete_name="Test Rider",
        generated_at=dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.UTC),
        period=Period(start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 30), label_zh="2026 年 9 月"),
        headline=DigestHeadline(text_zh="一致性 85 %，CTL +6。", verdict="on_track"),
        consistency=Consistency(planned=20, done=18, achieved=15),
    )
    c = compose_monthly(m, to_name="Roger", today=dt.date(2026, 10, 1))
    assert c.subject.startswith("【月報】2026 年 9 月")
    assert "一致性：計畫 20 堂、騎了 18 堂、達成 15 堂" in c.text
