"""compute_ride_metrics end-to-end on synthetic rides + the Explanation number invariant."""

from __future__ import annotations

import json
import re
from typing import Any

import polars as pl
import pytest

from cyp.analysis.ride.explain import GLOSSARY
from cyp.analysis.ride.pipeline import ALGO_VERSION, compute_ride_metrics
from cyp.analysis.ride.result import RideMetrics
from cyp.core.explain import Explanation
from tests.analysis.conftest import FTP, frame, inputs

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    return {float(x) for x in _NUM.findall(text)}


def _evidence_numbers(ev: dict[str, Any]) -> set[float]:
    out: set[float] = set()
    for v in ev.values():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int | float):
            out.add(float(v))
    return out


def assert_numbers_backed(expl: Explanation) -> None:
    """Every number quoted in prose must be present in that reason's evidence."""
    all_ev: set[float] = set()
    for r in expl.because:
        ev = _evidence_numbers(r.evidence)
        all_ev |= ev
        missing = _numbers(r.text_zh) - ev
        assert not missing, f"{r.text_zh!r} quotes {missing} not in evidence {r.evidence}"
    assert _numbers(expl.headline_zh) <= all_ev


def test_steady_ride_full_metrics(steady_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(steady_ride), inputs(cp=250.0, w_prime=20000.0))
    assert m.algo_version == ALGO_VERSION and m.tss_source == "power"
    assert m.np_w == pytest.approx(200.0) and m.if_ == pytest.approx(0.8)
    assert m.tss == pytest.approx(64.0) and m.vi == pytest.approx(1.0)
    assert m.ef == pytest.approx(200.0 / 140.0)
    assert m.avg_hr == pytest.approx(140.0)
    assert m.decoupling_pct == pytest.approx(0.0) and m.decoupling_reliable
    assert m.hr_lag_s is None  # constant power has no step to measure
    assert m.time_in_zone_power is not None and m.time_in_zone_power["Z3"] == 3600.0
    assert m.time_in_zone_hr is not None and m.time_in_zone_hr["Z2"] == 3600.0
    assert m.power_curve["60"] == pytest.approx(200.0)
    assert m.wbal_min_j == pytest.approx(20000.0)
    assert m.classification == "tempo"
    assert m.climbs == [] and m.efforts == []
    assert m.pacing["vi"] == 1.0 and m.pacing["split_pct"] == 0.0 and m.pacing["surges"] == 0
    # No lag -> NORMAL; TSS 64 < 150, decoupling 0 -> AS_PLANNED.
    assert (m.status, m.next_recommendation) == ("NORMAL", "AS_PLANNED")
    assert m.explanation is not None
    assert m.explanation.key == "ride:1"
    assert m.explanation.confidence == "high"
    assert m.explanation.method is not None
    assert m.explanation.method.model_id in GLOSSARY
    assert set(m.explanation.glossary_terms) <= set(GLOSSARY)
    assert {
        "coggan_np_if_tss",
        "decoupling_hr_lag",
        "efficiency_factor",
        "cp_wprime",
        "time_in_zone_tid",
    } <= set(m.explanation.glossary_terms)
    assert_numbers_backed(m.explanation)
    row = m.to_row()
    json.dumps(row)  # JSON-safe
    assert row["if_"] == pytest.approx(0.8) and row["explanation"]["key"] == "ride:1"


def test_comparison_deltas_vs_icu(steady_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(
        frame(steady_ride), inputs(icu_training_load=60.0, icu_np_w=205.0, icu_decoupling=1.0)
    )
    comp = m.comparison
    assert comp["tss_delta"] == pytest.approx(4.0) and comp["tss_rel_err"] == pytest.approx(
        4 / 60, abs=1e-3
    )
    assert comp["np_delta"] == pytest.approx(-5.0) and comp["np_rel_err"] == pytest.approx(
        -5 / 205, abs=1e-3
    )
    assert comp["ftp_used"] == FTP and comp["icu_decoupling"] == 1.0


def test_interval_ride_is_vo2_with_efforts(interval_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(interval_ride), inputs())
    assert m.classification == "vo2"
    assert sum(1 for e in m.efforts if e["kind"] == "effort") == 4
    assert m.pacing["structure"]["regular"] is True
    assert m.hr_lag_s is not None and m.hr_lag_s <= 5  # HR tracks power instantly here
    assert m.explanation is not None
    assert_numbers_backed(m.explanation)


def test_drifting_ride_flags_decoupling(drifting_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(drifting_ride), inputs())
    assert m.decoupling_pct == pytest.approx(5.71, abs=0.4) and m.decoupling_reliable
    assert m.hr_drift_detail is not None and m.hr_drift_detail["reliable"] is True
    assert (m.status, m.next_recommendation) == ("NORMAL", "EASY")  # decoupling >= 5 %
    assert m.explanation is not None
    assert_numbers_backed(m.explanation)


def test_climb_ride_has_climb_and_gain(climb_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(climb_ride), inputs())
    assert len(m.climbs) == 1 and m.climbs[0]["fingerprint"]
    assert m.elev_gain_m == pytest.approx(768.0, rel=0.03)
    assert m.explanation is not None
    assert any("爬坡" in r.text_zh for r in m.explanation.because)
    assert_numbers_backed(m.explanation)


def test_hr_only_ride_uses_hr_tss(hr_only_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(hr_only_ride), inputs(has_power=False))
    assert m.tss_source == "hr"
    assert m.tss == pytest.approx(100.0, abs=0.5)
    assert m.np_w is None and m.if_ is None and m.ef is None  # measured power fields stay empty
    assert m.time_in_zone_power is None
    assert m.estimated_power_meta is not None
    assert m.estimated_power_meta["power_estimated"] is True
    assert m.estimated_power_meta["np_est_w"] == pytest.approx(134.3, abs=1.5)
    assert m.hr_drift_detail is not None and m.hr_drift_detail["basis"] == "estimated"
    assert not m.decoupling_reliable
    assert m.explanation is not None and m.explanation.confidence == "low"
    assert any("hrTSS" in r.text_zh for r in m.explanation.because)
    assert_numbers_backed(m.explanation)


def test_no_power_no_hr_falls_back_to_estimated_tss() -> None:
    from tests.analysis.conftest import make_ride

    df = make_ride(3600, watts=None, hr=None)
    m = compute_ride_metrics(frame(df), inputs(has_power=False, has_hr=False))
    assert m.tss_source == "estimated"
    assert m.tss is not None and m.np_w is not None
    assert m.explanation is not None
    assert_numbers_backed(m.explanation)


def test_short_ride_has_no_np(steady_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(steady_ride.head(600)), inputs())
    assert m.np_w is None and m.tss is None
    assert m.explanation is not None and m.explanation.headline_zh
    assert m.comparison["tss_ours"] is None


def test_without_ftp_power_metrics_degrade(steady_ride: pl.DataFrame) -> None:
    m = compute_ride_metrics(frame(steady_ride), inputs(ftp=None, power_zones=None))
    assert m.np_w == pytest.approx(200.0) and m.if_ is None and m.tss is None
    assert m.time_in_zone_power is None and m.efforts == []
    assert isinstance(m, RideMetrics)
