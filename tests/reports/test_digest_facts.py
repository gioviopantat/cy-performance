"""build_weekly / build_monthly on an invented store (tests/reports/digest_world.py).

``tests/fixtures/digest_weekly.full.json`` and ``digest_monthly.full.json`` are the golden
outputs (every section present); the PDF renderer tests use them too. Regenerate after an
intended change with ``CYP_UPDATE_GOLDEN=1 uv run pytest tests/reports/test_digest_facts.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal import tid as tid_mod
from cyp.planning.adapt import OVERDONE_FACTOR
from cyp.reports import digest_facts, digest_monthly
from cyp.reports.digest_common import text, texts
from cyp.reports.digest_facts import build_monthly, build_weekly
from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest
from cyp.reports.digest_sections import TID_SESSION_SLACK, session_verdict, tid_verdict
from cyp.reports.ride_model import Intent
from cyp.settings import AthleteConfig, load_athlete_config
from cyp.store.db import make_engine, session_factory
from cyp.store.migrate import upgrade_head
from cyp.store.models import PlannedWorkout
from cyp.store.streams import StreamStore
from tests.conftest import ATHLETE_YAML
from tests.reports.digest_world import (
    EMPTY_MONTH,
    MONTH,
    WEEK_BREAK,
    WEEK_FULL,
    WEEK_OUTDOOR,
    WEEK_RECOVERY,
    seed_digest_world,
    trip_config,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO = FIXTURES.parents[1]
GOLDEN_WEEKLY = FIXTURES / "digest_weekly.full.json"
GOLDEN_MONTHLY = FIXTURES / "digest_monthly.full.json"
#: Monday 2026-11-02 07:00 Asia/Taipei: every week and October are over.
NOW = dt.datetime(2026, 11, 1, 23, 0, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, StreamStore, Path]]:
    """One invented store for the whole module (the builders only read)."""
    root = tmp_path_factory.mktemp("digest")
    url = f"sqlite:///{root / 'cyp.sqlite'}"
    upgrade_head(url)
    engine = make_engine(url)
    factory = session_factory(engine)
    store = StreamStore(root / "streams")
    seed_digest_world(factory, store)
    yield factory, store, root / "reports"
    engine.dispose()


@pytest.fixture
def cfg() -> AthleteConfig:
    return trip_config(load_athlete_config(ATHLETE_YAML))


@pytest.fixture(autouse=True)
def _fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(digest_facts, "now_utc", lambda: NOW)
    monkeypatch.setattr(digest_monthly, "RESEARCH_DIR", FIXTURES / "research")


def _due(day: dt.date) -> dt.datetime:
    """07:00 Taipei on ``day``: when the digest job sends (Monday / the 1st)."""
    return dt.datetime.combine(day, dt.time(7, 0), tzinfo=ZoneInfo("Asia/Taipei")).astimezone(
        dt.UTC
    )


def weekly(
    world: tuple[Any, StreamStore, Path],
    ws: dt.date,
    cfg: AthleteConfig | None,
    *,
    at: dt.datetime | None = None,
) -> WeeklyDigest:
    """Build the week as the job would on the following Monday (``at`` overrides the clock)."""
    factory: sessionmaker[Session] = world[0]
    when = at or _due(ws + dt.timedelta(days=7))
    with factory() as s, patch.object(digest_facts, "now_utc", lambda: when):
        return build_weekly(s, world[1], ws, profile="test", cfg=cfg, reports_dir=world[2])


def monthly(
    world: tuple[Any, StreamStore, Path],
    ms: dt.date,
    cfg: AthleteConfig | None,
    *,
    at: dt.datetime | None = None,
) -> MonthlyDigest:
    """Build the month as the job would on the 1st of the next month."""
    factory: sessionmaker[Session] = world[0]
    nxt = (ms.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    when = at or _due(nxt)
    with factory() as s, patch.object(digest_facts, "now_utc", lambda: when):
        return build_monthly(s, world[1], ms, profile="test", cfg=cfg, reports_dir=world[2])


def _explanations(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        if "headline_zh" in obj and "because" in obj:
            yield obj
        for v in obj.values():
            yield from _explanations(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _explanations(v)


# ----------------------------------------------------------------------------- weekly


def test_full_week(world: Any, cfg: AthleteConfig) -> None:
    d = weekly(world, WEEK_FULL, cfg)
    assert d.period.season_week == 1 and d.period.phase == "base" and not d.period.is_break

    # Section 2: one line per day, the per-ride intent verdicts, overdone and skipped added.
    got = [(s.day.isoformat(), s.verdict) for s in d.sessions]
    assert got == [
        ("2026-10-06", "achieved"),
        ("2026-10-07", "missed"),
        ("2026-10-08", "overdone"),
        ("2026-10-09", "skipped"),
        ("2026-10-10", "achieved"),
        ("2026-10-11", "unplanned"),
    ]
    over = d.sessions[2]
    assert over.actual_tss is not None and over.planned_tss == 20.0
    assert over.actual_tss / over.planned_tss > OVERDONE_FACTOR
    assert f"{OVERDONE_FACTOR:.0%}" in over.note_zh
    assert "3/3" in d.sessions[0].note_zh  # the intent check's own headline

    # Section 3: planned = the sessions' plans; daily rows add up; CTL from fitness_daily.
    assert d.load is not None
    assert d.load.planned_tss == 70 + 75 + 20 + 40 + 85
    assert d.load.actual_tss == pytest.approx(sum(a for _, _, a in d.load.daily), abs=0.1)
    assert len(d.load.daily) == 7 and len(d.load.ctl_series) == 7
    assert d.load.ctl_start is not None and d.load.ctl_end is not None
    assert d.load.ramp_per_week == pytest.approx(d.load.ctl_end - d.load.ctl_start, abs=0.1)
    assert d.load.ramp_cap == 6.0 and d.load.ctl_plan == []

    # Section 4: sessions by zone (tid_v2), every ride counted once, vs the base target.
    assert d.distribution is not None
    assert sum(d.distribution.sessions.values()) == 5
    assert d.distribution.target_frac == dict(
        zip(("low", "mid", "high"), tid_mod.PHASE_TARGETS["base"], strict=True)
    )
    assert d.distribution.verdict == "over"
    assert d.distribution.explanation is not None
    assert d.distribution.explanation["because"][-1]["evidence"]["slack"] == TID_SESSION_SLACK

    # Section 5: the 20-min effort at 262 W is this week's FTP evidence (x 0.95).
    assert d.goal is not None and d.goal.evidence_w == pytest.approx(262 * 0.95, abs=0.2)
    assert d.goal.projected_date is not None and d.goal.projected_date_before is not None

    # Section 6: 7-day HRV per day, readiness verdicts as stored, feel vs expected.
    assert d.recovery is not None
    assert all(x.hrv_z7 is not None for x in d.recovery.days)
    assert sum(d.recovery.readiness_counts.values()) == 7
    assert d.recovery.rest_days == 2
    gaps = {x.day: x.feel_gap for x in d.recovery.days if x.feel_gap is not None}
    assert set(gaps) == {dt.date(2026, 10, 6), dt.date(2026, 10, 7)}

    # Section 7: the climb's best ascent and a durability fade worse than the 4-week median.
    assert d.highlights is not None and d.highlights.climbs
    assert d.highlights.climbs[0].rank == 1
    assert d.highlights.durability_ratio is not None
    assert d.highlights.durability_median_ratio is not None
    assert d.highlights.durability_ratio < d.highlights.durability_median_ratio

    # Section 8: next week's stored plan with each session's purpose.
    assert d.next_week is not None
    assert [s.day.isoformat() for s in d.next_week.sessions] == [
        "2026-10-13",
        "2026-10-15",
        "2026-10-17",
    ]
    assert all(s.purpose_zh for s in d.next_week.sessions)

    # Section 1: deterministic from the sections; the one change is the overdone easy day.
    assert d.headline.verdict == "off_track"
    assert d.headline.change_zh.startswith(text("change.overdone", n=1, max=OVERDONE_FACTOR)[:8])
    assert d.headline.text_zh.startswith(text("headline.off_track", judged=5, counts=""))


def test_outdoor_week_names_stops_and_heat(world: Any, cfg: AthleteConfig) -> None:
    d = weekly(world, WEEK_OUTDOOR, cfg)
    heat = d.sessions[0]
    assert heat.outdoor is True and heat.verdict == "achieved"  # the red light was excused
    assert text("session.excused", n=1) in heat.note_zh
    stops = text("session.stops", n=9, minutes=0).split(text("common.list_sep"))[0]
    assert stops in heat.note_zh and text("session.heat", temp=32) in heat.note_zh
    town = d.sessions[1]
    assert text("session.stops", n=9, minutes=0).split(text("common.list_sep"))[0] in town.note_zh
    assert "°C" not in town.note_zh
    # The HRV dip of that week: 7-day mean under its band, REST mornings -> recovery first.
    assert d.recovery is not None and d.recovery.readiness_counts.get("REST", 0) >= 2
    assert d.headline.verdict == "on_track"
    assert d.headline.change_zh.startswith(text("change.recovery", rest=0, low=0)[:4])
    ids = [m.model_id for m in d.methods]
    assert "outdoor_context_v1" in ids and "climb_repeats" in ids


def test_break_week_is_the_short_version(world: Any, cfg: AthleteConfig) -> None:
    d = weekly(world, WEEK_BREAK, cfg)
    assert d.period.is_break
    assert "10/19–10/25" in d.period.break_reason_zh and "7" in d.period.break_reason_zh
    assert [s.verdict for s in d.sessions] == ["skipped", "skipped"]
    assert all(s.note_zh == text("session.skipped_unavailable") for s in d.sessions)
    assert d.load is not None and d.load.actual_tss == 0.0
    assert d.load.ctl_start is not None and d.load.ctl_end is not None
    assert d.load.ctl_end < d.load.ctl_start  # fitness drift
    assert d.distribution is d.goal is d.recovery is d.highlights is None
    assert d.next_week is not None and d.next_week.sessions[0].day == dt.date(2026, 10, 27)
    assert d.headline.verdict == "break"
    assert "10/27" in d.headline.change_zh
    assert [m.model_id for m in d.methods] == ["banister_pmc", "planner"]


def test_break_without_a_known_reason(world: Any) -> None:
    d = weekly(world, WEEK_BREAK, None)
    assert d.period.is_break and d.period.break_reason_zh == text("period.break_unknown")
    assert d.period.season_week is None and d.next_week is not None


def test_recovery_week_label(world: Any, cfg: AthleteConfig) -> None:
    d = weekly(world, WEEK_RECOVERY, cfg)
    assert text("period.recovery") in d.period.label_zh
    assert [s.verdict for s in d.sessions] == ["achieved", "achieved", "unplanned"]


def test_days_from_today_are_not_judged(world: Any, cfg: AthleteConfig) -> None:
    d = weekly(world, WEEK_FULL, cfg, at=dt.datetime(2026, 10, 8, 23, 0, tzinfo=dt.UTC))
    assert "skipped" not in {s.verdict for s in d.sessions}  # 10-09 is "today"


def test_backfilled_periods_have_no_next_plan(world: Any, cfg: AthleteConfig) -> None:
    """Built weeks after the fact, "next week" / "next month" is already over or under way."""
    late = _due(WEEK_FULL + dt.timedelta(days=21))
    assert weekly(world, WEEK_FULL, cfg, at=late).next_week is None
    assert weekly(world, WEEK_FULL, cfg).next_week is not None
    month = WEEK_FULL.replace(day=1)
    assert monthly(world, month, cfg, at=late + dt.timedelta(days=40)).next_month is None


def test_week_must_start_on_monday(world: Any, cfg: AthleteConfig) -> None:
    with pytest.raises(ValueError):
        weekly(world, WEEK_FULL + dt.timedelta(days=1), cfg)


# ----------------------------------------------------------------------------- monthly


def test_month_across_a_recovery_week(world: Any, cfg: AthleteConfig) -> None:
    d = monthly(world, MONTH, cfg)
    assert d.period.start == MONTH and d.period.end == dt.date(2026, 10, 31)
    assert d.period.label_zh == text("period.month", d=MONTH)

    c = d.consistency
    assert c is not None and (c.planned, c.done, c.achieved) == (12, 9, 6)
    weeks = {ws: (p, n, a) for ws, p, n, a in c.by_week}
    assert weeks[WEEK_BREAK] == (2, 0, 0) and weeks[WEEK_RECOVERY] == (2, 2, 2)
    assert c.longest_gap_days == 9  # 10-18 .. 10-26 (the trip)

    # Section 3: CTL over the month and the season plan's path at each week end.
    assert d.load is not None and d.load.ctl_plan
    assert d.load.ctl_plan[0][0] == dt.date(2026, 10, 4)
    assert all(b[1] > a[1] for a, b in zip(d.load.ctl_plan, d.load.ctl_plan[1:], strict=False))

    # Section 4: best power this month vs September (the climb history month).
    pc = d.power_curve
    assert pc is not None and pc.durations_s == [5, 60, 300, 1200, 3600]
    assert pc.this_month_w[3] == 262.0 and pc.last_month_w[3] is not None
    assert pc.best_since == MONTH  # the season starts inside the month

    # Sections 5-7.
    assert d.fitness is not None and d.fitness.goal is not None
    assert any("262" in e for e in d.fitness.backing_efforts_zh)
    assert [ws for ws, _ in d.distribution_by_week][1:] == [WEEK_FULL, WEEK_OUTDOOR, WEEK_RECOVERY]
    assert d.limiters is not None and d.limiters.lines[0].limiter_id == "aerobic_volume"
    assert (d.limiters.lines[0].rank_now, d.limiters.lines[0].rank_before) == (1, 1)

    # Section 8 / 9: recovery over the month, then November's stored plan and season marks.
    assert d.recovery is not None and len(d.recovery.days) == 31
    assert d.next_month is not None and len(d.next_month.sessions) == 2
    assert any(text("test.twenty_min") in k for k in d.next_month.key_sessions_zh)

    # Section 10: only the change the registry now carries counts as approved.
    assert d.research is not None
    assert d.research.report_path == "tests/fixtures/research/2026-10.md"
    assert d.research.grade_changes == [("banister_pmc.load_history", "C", "B")]

    assert d.headline.verdict == "partly"
    assert "12" in d.headline.text_zh and "9" in d.headline.text_zh


def test_month_without_rides(
    world: Any, cfg: AthleteConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(digest_facts, "now_utc", lambda: dt.datetime(2027, 1, 1, tzinfo=dt.UTC))
    d = monthly(world, EMPTY_MONTH, cfg)
    assert d.period.is_break and d.period.break_reason_zh == text("period.break_unknown")
    assert d.consistency is None  # nothing was planned: no consistency section
    assert d.power_curve is d.fitness is d.limiters is d.recovery is d.research is None
    assert d.distribution_by_week == []
    assert d.headline.verdict == "break" and d.headline.change_zh == ""
    assert d.next_month is not None and d.next_month.sessions == []


def test_month_must_start_on_the_first(world: Any, cfg: AthleteConfig) -> None:
    with pytest.raises(ValueError):
        monthly(world, dt.date(2026, 10, 2), cfg)


# ----------------------------------------------------------------------------- rules


def _intent(kind: str, ratio: float | None, verdict: str = "achieved") -> Intent:
    return Intent(
        planned_name="x",
        kind=kind,  # type: ignore[arg-type]
        purpose_zh="",
        target_zone="",
        target_min=0.0,
        achieved_min=0.0,
        load_ratio=ratio,
        verdict=verdict,  # type: ignore[arg-type]
    )


def test_overdone_only_for_easy_days_past_the_planner_factor() -> None:
    assert session_verdict(_intent("ceiling", OVERDONE_FACTOR + 0.01, "missed"), None) == "overdone"
    assert session_verdict(_intent("ceiling", OVERDONE_FACTOR), None) == "achieved"
    assert session_verdict(_intent("work", 2.0), None) == "achieved"  # a hard day ridden long
    easy = PlannedWorkout(intent="endurance")
    assert session_verdict(_intent("work", 2.0), easy) == "overdone"  # judged on load only
    assert session_verdict(_intent("work", None, "no_data"), None) == "no_data"
    assert session_verdict(None, None) == "unplanned"


def test_tid_verdict_allows_one_session_either_way() -> None:
    base = tid_mod.PHASE_TARGETS["base"]  # 20 % mid + high
    assert tid_verdict({"low": 4, "mid": 1, "high": 0}, base) == ("on", 1.0)
    assert tid_verdict({"low": 3, "mid": 1, "high": 1}, base) == ("on", 1.0)
    assert tid_verdict({"low": 2, "mid": 2, "high": 1}, base)[0] == "over"
    assert tid_verdict({"low": 10, "mid": 0, "high": 0}, base) == ("under", 2.0)
    assert tid_verdict({"low": 1, "mid": 0, "high": 0}, None) == ("no_target", None)


# ----------------------------------------------------------------------------- contract


def test_every_explanation_and_method_is_complete(world: Any, cfg: AthleteConfig) -> None:
    for d in (weekly(world, WEEK_FULL, cfg), monthly(world, MONTH, cfg)):
        expl = list(_explanations(d.model_dump(mode="json")))
        assert len(expl) >= 4
        assert all(e["headline_zh"] and e["because"] and e["method"] for e in expl)
        assert all((REPO / m.doc).is_file() for m in d.methods)
        assert all(m.grade is not None for m in d.methods if m.claim_id is not None)


def test_texts_have_no_unused_keys_in_use() -> None:
    # Every section of the yaml loads and formats; the verdict names cover the contract.
    assert set(texts()["verdict"]) >= {
        "achieved",
        "partial",
        "missed",
        "overdone",
        "skipped",
        "unplanned",
        "no_data",
    }


def test_golden_weekly(world: Any, cfg: AthleteConfig) -> None:
    got = weekly(world, WEEK_FULL, cfg).model_dump(mode="json")
    if os.environ.get("CYP_UPDATE_GOLDEN"):
        GOLDEN_WEEKLY.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", "utf-8")
    assert got == json.loads(GOLDEN_WEEKLY.read_text(encoding="utf-8"))


def test_golden_monthly(world: Any, cfg: AthleteConfig) -> None:
    got = monthly(world, MONTH, cfg).model_dump(mode="json")
    if os.environ.get("CYP_UPDATE_GOLDEN"):
        GOLDEN_MONTHLY.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", "utf-8")
    assert got == json.loads(GOLDEN_MONTHLY.read_text(encoding="utf-8"))


def test_golden_fixtures_are_complete() -> None:
    w = WeeklyDigest.model_validate_json(GOLDEN_WEEKLY.read_text(encoding="utf-8"))
    sections = [w.load, w.distribution, w.goal, w.recovery, w.highlights, w.next_week]
    assert all(x is not None for x in sections) and w.sessions and w.methods
    assert w.highlights is not None and w.highlights.climbs
    m = MonthlyDigest.model_validate_json(GOLDEN_MONTHLY.read_text(encoding="utf-8"))
    sections_m = [
        m.consistency,
        m.load,
        m.power_curve,
        m.fitness,
        m.limiters,
        m.recovery,
        m.next_month,
        m.research,
    ]
    assert all(x is not None for x in sections_m) and m.distribution_by_week and m.methods
