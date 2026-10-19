"""durability.py: EF, decoupling, HR lag, status/next thresholds."""

from __future__ import annotations

import inspect

import numpy as np
import polars as pl
import pytest

from cyp.analysis.ride import durability as d
from cyp.analysis.ride.durability import Thresholds, classify_status
from tests.analysis.conftest import frame, hr_zones, make_ride


def test_ef_and_hr_summary(steady_ride: pl.DataFrame) -> None:
    fr = frame(steady_ride)
    assert d.average_hr(fr) == pytest.approx(140.0)
    assert d.max_hr(fr) == pytest.approx(140.0)
    assert d.efficiency_factor(200.0, 140.0) == pytest.approx(200.0 / 140.0)
    assert d.efficiency_factor(None, 140.0) is None
    tiz = d.hr_time_in_zones(fr, hr_zones())
    assert tiz is not None and tiz["Z2"] == 3600.0


def test_decoupling_zero_on_steady_ride(steady_ride: pl.DataFrame) -> None:
    fr = frame(steady_ride)
    dec = d.decoupling(fr, fr.watts, basis="power", if_=0.8, vi=1.0)
    assert dec is not None
    assert dec.pct == pytest.approx(0.0)
    assert dec.reliable is True
    assert dec.excluded_warmup_s == 600
    assert dec.n_first_s + dec.n_second_s == 3000


def test_decoupling_hand_value_on_drift(drifting_ride: pl.DataFrame) -> None:
    # After the 10 min warm-up: HR_first = 130 + 20*1350/3600 = 137.5, HR_second = 145.83
    # EF_first = 200/137.5 = 1.4545, EF_second = 1.3714 -> 5.71 %
    fr = frame(drifting_ride)
    dec = d.decoupling(fr, fr.watts, basis="power", if_=0.8, vi=1.0)
    assert dec is not None
    # HR is stored as integers (Int16), so allow the truncation error.
    assert dec.hr_first == pytest.approx(137.5, abs=0.6)
    assert dec.hr_second == pytest.approx(145.83, abs=0.6)
    assert dec.pct == pytest.approx(5.71, abs=0.4)
    assert dec.reliable


def test_decoupling_unreliable_flags() -> None:
    fr = frame(make_ride(1800))
    dec = d.decoupling(fr, fr.watts, basis="power", if_=0.8, vi=1.0)
    assert dec is not None and not dec.reliable and "moving_s" in (dec.unreliable_reason or "")
    fr2 = frame(make_ride(3600))
    assert (
        d.decoupling(fr2, fr2.watts, basis="power", if_=0.95, vi=1.0).unreliable_reason == "IF>0.85"
    )  # type: ignore[union-attr]
    assert (
        d.decoupling(fr2, fr2.watts, basis="power", if_=0.8, vi=1.9).unreliable_reason == "VI>1.6"
    )  # type: ignore[union-attr]
    assert d.decoupling(fr2, fr2.speed, basis="speed", if_=None).unreliable_reason == "basis=speed"  # type: ignore[union-attr]


def test_decoupling_ignores_midride_stop() -> None:
    # Identical halves around a 10 min stop (0 W, HR 90): must be ~0, not skewed by the stop.
    w = np.concatenate([np.full(1500, 200.0), np.zeros(600), np.full(1500, 200.0)])
    hr = np.concatenate([np.full(1500, 140.0), np.full(600, 90.0), np.full(1500, 140.0)])
    fr = frame(make_ride(3600, watts=w, hr=hr, stops=[(1500, 2100)]))
    dec = d.decoupling(fr, fr.watts, basis="power", if_=0.8, vi=1.0)
    assert dec is not None and abs(dec.pct) < 0.5


def test_hr_lag_recovers_planted_delay() -> None:
    # Square-wave power (5 min on / 5 min off); HR is the same wave delayed by 40 s + smoothing.
    n = 3600
    w = np.where((np.arange(n) // 300) % 2 == 0, 250.0, 120.0)
    hr_raw = 120.0 + (w - 120.0) * 0.3
    hr = np.roll(hr_raw, 40)
    hr[:40] = hr_raw[0]
    fr = frame(make_ride(n, watts=w, hr=hr))
    lag = d.hr_lag(fr)
    assert lag is not None
    assert lag.lag_s == pytest.approx(40, abs=2)
    assert lag.corr > 0.9


def test_hr_lag_none_without_signal(steady_ride: pl.DataFrame) -> None:
    assert d.hr_lag(frame(steady_ride)) is None  # constant power: no response to measure
    rng = np.random.default_rng(3)
    noisy = frame(
        make_ride(1800, watts=rng.uniform(100, 300, 1800), hr=rng.uniform(120, 160, 1800))
    )
    assert d.hr_lag(noisy) is None  # uncorrelated noise fails lag_min_corr
    short = frame(make_ride(300))
    assert d.hr_lag(short) is None  # under lag_min_samples


@pytest.mark.parametrize(
    ("tss", "expected"),
    [
        (None, ("NORMAL", "AS_PLANNED")),
        (60.0, ("NORMAL", "AS_PLANNED")),
        (149.0, ("NORMAL", "AS_PLANNED")),
        (160.0, ("NORMAL", "EASY")),  # TSS >= 150
        (260.0, ("OVERREACHED", "EASY")),
        (320.0, ("OVERREACHED", "REST")),
    ],
)
def test_classify_status(tss: float | None, expected: tuple[str, str]) -> None:
    assert classify_status(tss=tss) == expected


def test_status_ignores_decoupling_and_hr_lag() -> None:
    """ADR-0012: decoupling and HR lag never decide the status or tomorrow's session."""
    params = inspect.signature(classify_status).parameters
    assert set(params) == {"tss", "thresholds"}
    keys = Thresholds().as_dict()
    assert not [k for k in keys if k.startswith(("fresh_", "blunted_", "upgrade_"))]
    assert "overreached_min_decoupling" not in keys and "easy_min_decoupling" not in keys


def test_thresholds_are_one_dataclass() -> None:
    th = Thresholds(easy_min_tss=40.0)
    assert classify_status(tss=50.0, thresholds=th) == ("NORMAL", "EASY")
    assert th.as_dict()["easy_min_tss"] == 40.0
