"""build_ride_report on an invented store: every section on / None, and the golden report.

``tests/fixtures/ride_report.full.json`` is the golden output (all sections present); the PDF
renderer tests use it too. Regenerate after an intended change with
``CYP_UPDATE_GOLDEN=1 uv run pytest tests/reports/test_ride_facts.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.readiness import RPE_SURPRISE
from cyp.analysis.ride import context as ctx_mod
from cyp.core.errors import NotFoundError
from cyp.reports import ride_facts
from cyp.reports.ride_common import texts
from cyp.reports.ride_facts import _start_local, build_ride_report, text
from cyp.reports.ride_goal import FTP_FROM_20MIN, ride_evidence
from cyp.reports.ride_model import RideReport, Tomorrow
from cyp.services.ride_preview import shift_to_tomorrow
from cyp.settings import AthleteConfig, load_athlete_config
from cyp.store.models import Activity, PlannedWorkout
from cyp.store.streams import StreamStore
from tests.conftest import ATHLETE_YAML
from tests.reports.world import RIDE_DAY, seed_world

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = FIXTURES / "ride_report.full.json"
TOMORROW = FIXTURES / "ride_report.tomorrow.json"
NOW = dt.datetime(2026, 10, 14, 11, 30, tzinfo=dt.UTC)
RATE_URL = "http://127.0.0.1:8765/rides/feel"


@pytest.fixture
def cfg() -> AthleteConfig:
    return load_athlete_config(ATHLETE_YAML)


@pytest.fixture
def store(data_dir: Path) -> StreamStore:
    return StreamStore(data_dir / "streams")


@pytest.fixture
def world(factory: sessionmaker[Session], store: StreamStore) -> dict[str, int]:
    return seed_world(factory, store)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ride_facts, "now_utc", lambda: NOW)


def report(
    factory: sessionmaker[Session],
    store: StreamStore,
    activity_id: int,
    cfg: AthleteConfig | None,
    **kw: object,
) -> RideReport:
    with factory() as s:
        return build_ride_report(
            s, store, activity_id, profile="test", cfg=cfg, rate_url=RATE_URL, **kw
        )  # type: ignore[arg-type]


def tomorrow() -> Tomorrow:
    return Tomorrow.model_validate(json.loads(TOMORROW.read_text(encoding="utf-8")))


# ----------------------------------------------------------------------------- full ride


def test_outdoor_ride_with_stops_and_heat(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    r = report(factory, store, world["ride"], cfg, tomorrow=tomorrow())
    assert r.ride.indoor is False and r.ride.tss_source == "cyp"

    # Section 2: three reps, the red light inside rep 2 excused, not a failure.
    assert r.intent is not None
    assert (r.intent.reps_planned, r.intent.reps_counted) == (3, 3)
    assert r.intent.verdict == "achieved"
    assert (r.intent.target_low_w, r.intent.target_high_w) == (212, 238)  # 85-95 % outdoors
    assert len(r.intent.excused) == 1 and r.intent.excused[0].start_s < 2040
    assert r.intent.load_ratio == pytest.approx(r.intent.load_actual / 75.0, abs=1e-3)
    assert r.intent.explanation is not None
    assert r.intent.explanation["method"]["inputs"]["achieved_frac"] == 0.9

    # Section 4: nine stops, heat and stop caveats with the measured numbers.
    assert r.outdoor is not None
    assert r.outdoor.stops_n == 9 and r.outdoor.temp_avg_c is not None
    assert r.outdoor.temp_avg_c >= ctx_mod.HEAT_AVG_C
    assert len(r.outdoor.caveats_zh) == 2
    assert f"{r.outdoor.temp_avg_c:.0f}" in r.outdoor.caveats_zh[0]

    # Section 5: the same climb ridden four times before, cooler -> heat note.
    assert len(r.climbs) == 1
    c = r.climbs[0]
    assert (c.rank, c.of_n, len(c.history)) == (4, 5, 4)
    assert c.this.kj_before is not None and c.this.temp_c == 31.0
    assert all(h.temp_c == 22.0 for h in c.history)
    assert "9 °C" in c.note_zh

    # Section 3: rising icu eFTP -> a date before the goal date, unchanged since yesterday.
    assert r.goal is not None
    assert r.goal.current_ftp_w == 250.0 and r.goal.target_w == 285.0
    assert r.goal.projected_date is not None and r.goal.on_track is True
    assert r.goal.projected_shift_days == 0
    assert any(p.source == "icu_eftp" for p in r.goal.history)

    # Sections 6-9.
    assert r.feel.rated and r.feel.rpe == 7.0 and r.feel.expected_rpe == 7.0
    assert r.limiters is not None and r.limiters.lines[0].limiter_id == "aerobic_volume"
    assert r.limiters.lines[0].week_min > r.limiters.lines[0].trained_min > 0
    assert r.tomorrow is not None and r.tomorrow.changed
    ids = [m.model_id for m in r.methods]
    assert ids[:3] == ["coggan_np_if_tss", "ride_intent_v1", "goal_projection_v2"]
    graded = {m.model_id: m for m in r.methods if m.claim_id is not None}
    assert graded["coggan_np_if_tss"].grade == "B"
    assert graded["ride_intent_v1"].grade == "C"
    assert all(m.citation for m in graded.values())
    assert all((Path(__file__).resolve().parents[2] / m.doc).is_file() for m in r.methods)

    # Section 1 is built from the verdicts, deterministically.
    assert r.headline.verdict == "achieved"
    assert "3/3" in r.headline.text_zh and r.goal.projected_date.isoformat() in r.headline.text_zh

    # Charts: equal-length 10 s series.
    assert r.series is not None and r.series.step_s == 10
    lengths = {len(r.series.power_w), len(r.series.hr_bpm), len(r.series.altitude_m)}
    assert lengths == {len(r.series.temp_c)}


def test_golden_full_report(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    got = report(factory, store, world["ride"], cfg, tomorrow=tomorrow()).model_dump(mode="json")
    if os.environ.get("CYP_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert got == want


def test_golden_fixture_is_a_valid_complete_report() -> None:
    r = RideReport.model_validate_json(GOLDEN.read_text(encoding="utf-8"))
    present = [r.intent, r.goal, r.outdoor, r.limiters, r.tomorrow, r.series]
    assert all(x is not None for x in present) and r.climbs and r.methods and r.feel.rated


# ----------------------------------------------------------------------------- variants


def test_indoor_unplanned_ride(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    r = report(factory, store, world["indoor"], cfg)
    assert r.ride.indoor is True
    assert r.outdoor is None and r.climbs == [] and r.intent is None
    assert r.headline.verdict == "unplanned"
    assert r.tomorrow is None
    # Unrated: section 6 links to the rating page instead.
    assert r.feel.rated is False and r.feel.rate_url == RATE_URL
    assert r.feel.text_zh == text("feel.unrated")


def test_web_feedback_wins_over_icu(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    r = report(factory, store, world["ride"], cfg, feedback=(10, None))
    assert r.feel.rpe == 10.0 and r.feel.feel == 2  # feel still from icu
    assert r.feel.gap == 3.0
    # gap >= RPE_SURPRISE: the "much harder" sentence, quoting both RPEs.
    assert r.feel.gap >= RPE_SURPRISE
    first = r.feel.text_zh.split(text("common.sep"))[0]
    assert "RPE 10" in first and "7.0" in first and f"{RPE_SURPRISE:.0f}" in first


def test_no_power_band_judges_load_only(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    with factory() as s:
        pw = s.query(PlannedWorkout).one()
        pw.template_id = "unknown_template"
        s.commit()
    r = report(factory, store, world["ride"], cfg)
    assert r.intent is not None
    assert r.intent.target_min == 0.0 and r.intent.reps_counted is None
    assert r.intent.verdict == "achieved"  # load ratio > 0.9


def test_recovery_plan_is_judged_by_its_ceiling(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    with factory() as s:
        pw = s.query(PlannedWorkout).one()
        pw.template_id, pw.intent = "recovery_spin", "recovery"
        pw.steps = {"params": {"total_min": 45}}
        s.commit()
    r = report(factory, store, world["ride"], cfg)
    assert r.intent is not None and r.intent.reps_planned is None
    assert r.intent.target_high_w == 150  # (55 % + 5 % outdoor widening) of 250 W
    assert r.intent.verdict == "missed"  # sweet spot on a recovery day
    inputs = (r.intent.explanation or {})["method"]["inputs"]
    assert r.intent.kind == "ceiling" and inputs["over_share"] > 0.25
    assert inputs["smooth_s"] == 90
    # The report headline quotes the intent section's own headline (same numbers).
    assert (r.intent.explanation or {})["headline_zh"] in r.headline.text_zh


def test_without_power_the_intent_is_no_data(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    df = store.read(world["ride"]).drop("watts")
    store.write(world["ride"], df)
    r = report(factory, store, world["ride"], cfg)
    assert r.intent is not None and r.intent.verdict == "no_data"
    assert r.headline.verdict == "no_data"


def test_insufficient_goal_data_gives_no_date(
    factory: sessionmaker[Session], store: StreamStore, cfg: AthleteConfig
) -> None:
    ids = seed_world(factory, store, eftp_days=14)
    r = report(factory, store, ids["ride"], cfg)
    assert r.goal is not None
    assert r.goal.projected_date is None and r.goal.on_track is None
    assert r.goal.projected_shift_days is None
    expl = r.goal.explanation or {}
    assert expl["confidence"] == "low"
    assert any(
        str(goal_mod.MIN_WEEKS) in b["text_zh"] for b in expl["because"]
    )  # the minimum-data rule is quoted
    assert text("headline.goal_none", target=285.0) in r.headline.text_zh


def test_without_config_there_is_no_goal(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int]
) -> None:
    r = report(factory, store, world["ride"], None)
    assert r.goal is None
    assert r.intent is not None  # the plan does not need athlete.yaml


def test_missing_streams(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    store.delete(world["ride"])
    r = report(factory, store, world["ride"], cfg)
    assert r.series is None and r.outdoor is None
    assert r.intent is not None and r.intent.verdict == "no_data"


def test_second_ride_of_the_day_does_not_take_the_plan(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    with factory() as s:
        s.add(
            Activity(
                athlete_id=1,
                sport_type="Ride",
                is_ride=True,
                name="coffee spin",
                start_utc=f"{RIDE_DAY.isoformat()}T09:00:00Z",
                start_local=f"{RIDE_DAY.isoformat()}T17:00:00",
                tz="Asia/Taipei",
                moving_s=1200,
            )
        )
        s.commit()
        extra = s.query(Activity).filter_by(name="coffee spin").one().id
    assert report(factory, store, extra, cfg).intent is None
    assert report(factory, store, world["ride"], cfg).intent is not None


def test_unknown_activity(factory: sessionmaker[Session], store: StreamStore) -> None:
    with factory() as s, pytest.raises(NotFoundError):
        build_ride_report(s, store, 404, profile="test", cfg=None)


def test_heat_is_judged_against_the_usual_temperature(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    r = report(factory, store, world["ride"], cfg)
    assert r.outdoor is not None
    heat = r.outdoor.caveats_zh[0]
    # 32 °C today vs ~23 °C on the four cooler rides of the last 28 days.
    assert "32 °C" in heat and "23 °C" in heat and "9 °C" in heat


def test_unplanned_outdoor_ride_does_not_mention_excused_reps(
    factory: sessionmaker[Session], store: StreamStore, world: dict[str, int], cfg: AthleteConfig
) -> None:
    with factory() as s:
        s.query(PlannedWorkout).delete()
        s.commit()
    r = report(factory, store, world["ride"], cfg)
    assert r.intent is None and r.outdoor is not None
    stops = [c for c in r.outdoor.caveats_zh if f"{r.outdoor.stops_n} " in c]
    assert stops and not stops[0].endswith(text("outdoor.stops_reps"))


def test_ride_ftp_evidence_reuses_max_effort_evidence() -> None:
    row = SimpleNamespace(id=7, power_ok=True, power_curve={1200: 270.0})
    status = SimpleNamespace(
        current_ftp=250.0,
        max_efforts=SimpleNamespace(
            sustained=[
                {"activity_id": 7, "kind": "power_20min", "watts": 262, "label_zh": "a"},
                {"activity_id": 7, "kind": "test", "label_zh": "b"},
                {"activity_id": 8, "kind": "power_20min", "watts": 300, "label_zh": "c"},
            ]
        ),
    )
    inp = SimpleNamespace(row=row)
    ev = ride_evidence(inp, status)  # type: ignore[arg-type]
    assert ev == (round(270.0 * FTP_FROM_20MIN, 1), "b")  # the test's 20-min power wins
    status.max_efforts.sustained = []
    assert ride_evidence(inp, status) is None  # type: ignore[arg-type]


def test_start_falls_back_to_utc_in_the_ride_timezone() -> None:
    a = Activity(start_utc="2026-10-13T23:00:00Z", start_local=None, tz="Asia/Taipei")
    assert _start_local(a) == dt.datetime(2026, 10, 14, 7, 0)


def test_planner_sentences_are_shifted_to_tomorrow() -> None:
    words = texts()["tomorrow_shift"]
    yesterday, today = list(words)
    assert shift_to_tomorrow(f"{yesterday} 1 {today} 2") == f"{words[yesterday]} 1 {words[today]} 2"


FORBIDDEN = json.loads((FIXTURES / "ride_report.tomorrow_forbidden.json").read_text("utf-8"))


def test_tomorrow_reasons_read_for_tomorrow() -> None:
    """Section 8 speaks about tomorrow; the ride just done is today's ride, never yesterday."""
    golden = RideReport.model_validate_json(GOLDEN.read_text(encoding="utf-8"))
    assert golden.tomorrow is not None and golden.tomorrow.reasons_zh
    for reason in golden.tomorrow.reasons_zh:
        assert not any(p in reason for p in FORBIDDEN["phrases"]), reason
    words = texts()["tomorrow_shift"]
    yesterday, today = list(words)
    planner = f"{yesterday} 132 %, {today} -> x"
    shifted = shift_to_tomorrow(planner)
    assert not any(p in shifted for p in FORBIDDEN["phrases"]), shifted
