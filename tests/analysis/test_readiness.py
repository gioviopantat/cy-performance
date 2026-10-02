"""readiness_v1: components, renormalisation, rules, the glossary worked example."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from cyp.analysis import readiness as rd

DAY = dt.date(2026, 10, 2)


def _history(n: int = 30, **overrides: float) -> list[rd.WellnessPoint]:
    """Baseline with known mean/sd: alternating values around a centre."""
    out = []
    for i in range(n):
        sign = 1 if i % 2 else -1
        out.append(
            rd.WellnessPoint(
                date=DAY - dt.timedelta(days=n - i),
                hrv=math.exp(math.log(60) + 0.1 * sign),
                resting_hr=48 + 2 * sign,
                sleep_s=7 * 3600 + 1800 * sign,
                sleep_score=80 + 5 * sign,
                **overrides,
            )
        )
    return out


def test_glossary_worked_example() -> None:
    """docs/glossary/readiness_v1.md: S = −0.31, score 42 -> EASY."""
    comps = [
        rd.Component("hrv", -0.6, 0.30),
        rd.Component("rhr", -0.4, 0.15),
        rd.Component("sleep", 0.5, 0.20),
        rd.Component("tsb", rd.tsb_component(-14) or 0.0, 0.15),
        rd.Component("ride", -0.5, 0.20),
    ]
    s, score = rd.aggregate(comps)
    assert s == pytest.approx(-0.31, abs=0.005)
    assert round(score) == 42


def test_tsb_and_ride_components() -> None:
    assert rd.tsb_component(-30) == pytest.approx(-1)
    assert rd.tsb_component(10) == pytest.approx(1)
    assert rd.tsb_component(-90) == -2.0
    assert rd.tsb_component(None) is None
    z, parts = rd.ride_component(
        rd.YesterdayRide(decoupling_pct=6.5, decoupling_reliable=True, load=108, planned_load=100)
    )
    assert parts == {"decoupling": pytest.approx(-0.5), "load_vs_plan": 0.0}
    assert z == pytest.approx(-0.25)
    unreliable, _ = rd.ride_component(rd.YesterdayRide(decoupling_pct=12))
    assert unreliable is None


def test_neutral_day_is_as_planned_and_missing_renormalised() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=60.0, resting_hr=48, sleep_s=7 * 3600, sleep_score=80)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=0.0))
    assert r.score_0_100 == pytest.approx(50.0, abs=0.5)
    assert r.recommendation == "AS_PLANNED" and r.status == "NORMAL"
    assert set(r.inputs["missing"]) == {"ride", "subjective"}
    weights = [x.weight for x in r.explanation.because if x.weight is not None]
    assert sum(weights) == pytest.approx(1.0, abs=0.01)  # renormalised shares
    assert r.explanation.confidence == "high"


def test_good_day_upgrades_unless_yesterday_was_hard() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=72.0, resting_hr=45, sleep_s=8 * 3600, sleep_score=88)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=5.0))
    assert r.score_0_100 >= 65 and r.recommendation == "UPGRADE" and r.status == "FRESH"
    hard = rd.YesterdayRide(classification="vo2")
    r2 = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=5.0, yesterday=hard))
    assert r2.recommendation == "AS_PLANNED"


def test_hrv_rhr_rule_forces_rest() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=48.0, resting_hr=53, sleep_s=7 * 3600, sleep_score=80)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=0.0))
    assert r.recommendation == "REST" and r.status == "OVERREACHED"
    assert r.inputs["rule_hits"] and "HRV" in r.explanation.because[0].text_zh


def test_sick_event_and_injury_force_rest() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=70.0, resting_hr=46)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, sick_or_injured="SICK"))
    assert (r.recommendation, r.status) == ("REST", "SICK")
    hurt = rd.WellnessPoint(DAY, hrv=70.0, injury=3)
    assert rd.compute_readiness(rd.ReadinessInputs(DAY, hurt, hist)).recommendation == "REST"


def test_blunted_and_overload_cap_at_easy() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=72.0, resting_hr=45, sleep_s=8 * 3600, sleep_score=88)
    blunted = rd.YesterdayRide(status="BLUNTED", hr_lag_s=95)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=5.0, yesterday=blunted))
    assert (r.recommendation, r.status) == ("EASY", "BLUNTED")
    over = rd.YesterdayRide(load=150, planned_load=100)
    r2 = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=5.0, yesterday=over))
    assert r2.recommendation == "EASY"


def test_short_baseline_and_no_data() -> None:
    today = rd.WellnessPoint(DAY, hrv=40.0, resting_hr=60)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, _history(n=10)))
    assert r.score_0_100 == 50.0 and r.explanation.confidence == "low"
    assert r.recommendation == "AS_PLANNED"
    empty = rd.compute_readiness(rd.ReadinessInputs(DAY, None))
    assert empty.inputs["missing"] == list(rd.WEIGHTS)


def test_subjective_direction_lower_is_better() -> None:
    hist = _history(soreness=1.0, fatigue=2.0)
    for i, h in enumerate(hist):
        h.soreness = 1.0 + (i % 2)
        h.fatigue = 2.0 + (i % 2)
    sore = rd.WellnessPoint(DAY, soreness=4, fatigue=4)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, sore, hist))
    assert r.inputs["components"]["subjective"]["z"] < 0


def test_wellness_coverage() -> None:
    cov = rd.wellness_coverage([{"hrv": 50, "sleep_s": None}, {"hrv": 51, "sleep_s": 100}])
    assert cov["hrv"] == 2 and cov["sleep_s"] == 1 and cov["mood"] == 0
