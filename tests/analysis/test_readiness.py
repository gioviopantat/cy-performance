"""readiness_v2: components, renormalisation, rules, the glossary worked example."""

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
    z, parts = rd.ride_component(rd.YesterdayRide(load=130, planned_load=100, feel=3))
    assert parts == {"load_vs_plan": pytest.approx(-1.0), "feel": 0.0}
    assert z == pytest.approx(-0.5)
    nothing, _ = rd.ride_component(rd.YesterdayRide(classification="endurance"))
    assert nothing is None


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


def _low_week(hist: list[rd.WellnessPoint], hrv: float) -> list[rd.WellnessPoint]:
    for h in hist[-6:]:
        h.hrv = hrv
    return hist


def test_hrv_rhr_rule_forces_rest() -> None:
    """A week-long HRV drop plus a high resting HR today -> REST."""
    hist = _low_week(_history(), 45.0)
    today = rd.WellnessPoint(DAY, hrv=45.0, resting_hr=53, sleep_s=7 * 3600, sleep_score=80)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=0.0))
    assert r.recommendation == "REST" and r.status == "OVERREACHED"
    assert r.inputs["rule_hits"] and "HRV 7 日平均" in r.explanation.because[0].text_zh
    assert r.inputs["components"]["hrv"]["band"] == "below"


def test_single_low_night_does_not_force_rest() -> None:
    """v1 rested on one low night; v2 reads the 7-day mean, which one night barely moves."""
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=40.0, resting_hr=53, sleep_s=7 * 3600, sleep_score=80)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, tsb=0.0))
    assert r.recommendation != "REST"
    hrv = r.inputs["components"]["hrv"]
    assert hrv["raw_z"] == pytest.approx(-0.58, abs=0.01)  # one night at -4 SD, averaged
    assert hrv["z"] == pytest.approx(-0.16, abs=0.01)  # just outside the band, far from -1.5


def test_hrv_window_band_and_minimum_nights() -> None:
    hist = _history()
    w = rd.hrv_window(DAY, 60.0, hist)
    assert w is not None and w.n_days == 7 and w.band == "within" and w.component == 0.0
    # z7 = -1.0 is one unit beyond the band; -1.25 is the REST line.
    assert rd.HrvWindow(-1.0, 0, 7, 0, 1).component == pytest.approx(-1.0)
    assert rd.HrvWindow(-1.25, 0, 7, 0, 1).component == pytest.approx(rd.HRV_REST_COMPONENT)
    assert rd.HrvWindow(0.75, 0, 7, 0, 1).component == pytest.approx(0.5)
    assert rd.HrvWindow(-3.0, 0, 7, 0, 1).component == -2.0  # clipped
    # Missing nights are skipped; fewer than three valid nights -> no HRV component.
    sparse = [h for h in hist if h.date < DAY - dt.timedelta(days=6)]
    sparse.append(rd.WellnessPoint(DAY - dt.timedelta(days=2), hrv=60.0))
    assert rd.hrv_window(DAY, 60.0, sparse) is None
    sparse.append(rd.WellnessPoint(DAY - dt.timedelta(days=4), hrv=60.0))
    three = rd.hrv_window(DAY, 60.0, sparse)
    assert three is not None and three.n_days == 3
    assert rd.hrv_window(DAY, None, sparse) is None  # today missing leaves two nights


def test_sick_event_and_injury_force_rest() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=70.0, resting_hr=46)
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist, sick_or_injured="SICK"))
    assert (r.recommendation, r.status) == ("REST", "SICK")
    hurt = rd.WellnessPoint(DAY, hrv=70.0, injury=3)
    assert rd.compute_readiness(rd.ReadinessInputs(DAY, hurt, hist)).recommendation == "REST"


def test_overload_caps_at_easy() -> None:
    hist = _history()
    today = rd.WellnessPoint(DAY, hrv=72.0, resting_hr=45, sleep_s=8 * 3600, sleep_score=88)
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


def test_stored_z_is_direction_normalised_not_raw() -> None:
    """Regression: the raw z of resting HR used to overwrite the normalised z in inputs."""
    hist = _history()
    today = rd.WellnessPoint(DAY, resting_hr=52)  # 2 sd above baseline -> bad
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, today, hist))
    comp = r.inputs["components"]["rhr"]
    assert comp["z"] == pytest.approx(-2.0) and comp["raw_z"] == pytest.approx(2.0)
    text = r.explanation.because[0].text_zh
    assert text.startswith("安靜心率比你的基準高 2.0 個標準差") and "不利" in text


def test_sparse_day_shrinks_toward_neutral() -> None:
    """ADR-0016: TSB alone (weight 0.15) no longer decides the day on its own."""
    s, score = rd.aggregate([rd.Component("tsb", -1.287, 0.15)])
    assert s == pytest.approx(0.15 * -1.287 / rd.MIN_TOTAL_WEIGHT)
    assert score == pytest.approx(40.3, abs=0.1)  # EASY, not the 17.8 (REST) of full renorm
    two = [rd.Component("rhr", -2.0, 0.15), rd.Component("tsb", -0.542, 0.15)]
    assert rd.aggregate(two)[0] == pytest.approx((-0.30 - 0.0813) / 0.5, abs=1e-3)
    full = [rd.Component("hrv", -1.0, 0.30), rd.Component("sleep", -1.0, 0.20)]
    assert rd.aggregate(full)[0] == pytest.approx(-1.0)  # at 0.5 nothing is shrunk
    r = rd.compute_readiness(rd.ReadinessInputs(DAY, None, tsb=-38.6))
    assert r.recommendation == "EASY"
    assert any("當成中性 0" in b.text_zh for b in r.explanation.because if b.text_zh)
