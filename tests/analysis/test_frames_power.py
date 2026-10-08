"""frames.py + power.py on synthetic rides."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from cyp.analysis.ride import power
from cyp.analysis.ride.frames import (
    RideFrame,
    elevation_gain,
    rolling_mean,
    smooth_altitude,
)
from cyp.core.errors import AnalysisError
from tests.analysis.conftest import FTP, WEIGHT, frame, make_ride


def test_frame_masks_recorded_vs_moving() -> None:
    df = make_ride(600, pauses=[(100, 160)], stops=[(300, 330)])
    fr = frame(df)
    assert fr.n == 600
    assert fr.recording_s == 540  # 60 paused seconds carry no data
    assert fr.moving_s == 510  # ... and 30 stopped seconds are not moving
    assert fr.elapsed_s == 599
    assert fr.watts is not None and np.isnan(fr.watts[120])


def test_frame_uses_strava_moving_flag_when_present() -> None:
    df = make_ride(100)
    flag = [i >= 20 for i in range(100)]
    df = df.with_columns(pl.Series("moving", flag))
    fr = frame(df)
    assert fr.moving_s == 80


def test_frame_rejects_non_contiguous_grid() -> None:
    df = make_ride(10).filter(pl.col("t_s") != 5)
    with pytest.raises(AnalysisError):
        RideFrame.from_df(df)


def test_rolling_mean_and_smoothing() -> None:
    r = rolling_mean(np.arange(10, dtype=float), 3)
    assert np.isnan(r[0]) and np.isnan(r[1])
    assert r[2] == pytest.approx(1.0) and r[-1] == pytest.approx(8.0)
    rng = np.random.default_rng(1)
    noisy = 100.0 + rng.uniform(-8, 8, size=3600)
    assert elevation_gain(smooth_altitude(noisy)) < 50.0  # raw diff sum would be ~hundreds


def test_steady_ride_power_metrics(steady_ride: pl.DataFrame) -> None:
    fr = frame(steady_ride)
    pm = power.compute_power_metrics(
        fr, ftp=FTP, weight_kg=WEIGHT, zones=power.coggan_zones(FTP), cp=250.0, w_prime=20000.0
    )
    assert pm.np_w == pytest.approx(200.0) and pm.np_recorded_w == pytest.approx(200.0)
    assert pm.if_ == pytest.approx(0.8)
    assert pm.tss == pytest.approx(64.0)  # 1 h * 0.8^2 * 100
    assert pm.vi == pytest.approx(1.0)
    assert pm.kj == pytest.approx(720.0)
    assert pm.avg_w == pytest.approx(200.0) and pm.max_w == pytest.approx(200.0)
    assert set(pm.power_curve) == set(power.POWER_CURVE_DURATIONS) - {5400}
    assert all(v == pytest.approx(200.0) for v in pm.power_curve.values())
    assert pm.power_curve_wkg[60] == pytest.approx(200.0 / WEIGHT)
    assert pm.time_in_zone is not None
    assert pm.time_in_zone["Z3"] == 3600.0 and sum(pm.time_in_zone.values()) == 3600.0
    assert pm.wbal_min_j == pytest.approx(20000.0)  # never above CP


def test_np_requires_one_minute() -> None:
    # Was 20 min; short rides (Rouvy segments, warm-ups) need a load too (ride-1.2.0).
    assert power.normalized_power(np.full(59, 220.0)) is None
    assert power.normalized_power(np.full(60, 220.0)) == pytest.approx(220.0)


def test_np_weights_surges(interval_ride: pl.DataFrame) -> None:
    fr = frame(interval_ride)
    p = power.recorded_power(fr)
    assert p is not None
    np_w = power.normalized_power(p)
    assert np_w is not None and np_w > p.mean()
    assert power.variability_index(np_w, float(p.mean())) > 1.1


def test_power_curve_picks_best_window() -> None:
    w = np.full(600, 100.0)
    w[100:160] = 400.0
    curve, _ = power.power_curve(w, durations=(5, 60, 300, 3600))
    assert curve[5] == pytest.approx(400.0) and curve[60] == pytest.approx(400.0)
    assert 100.0 < curve[300] < 400.0
    assert 3600 not in curve


def test_zones_from_pct_bounds_matches_icu_format() -> None:
    zones = power.zones_from_pct_bounds(265.0, [55, 75, 90, 105, 120, 150, 999])
    assert zones is not None and len(zones.zones) == 7
    assert zones.zones[0].hi == pytest.approx(145.8)
    assert zones.zones[-1].hi is None
    tiz = power.time_in_zones(np.array([0.0, 100.0, 150.0, 400.0, np.nan]), zones)
    assert tiz == {"Z1": 2.0, "Z2": 1.0, "Z3": 0.0, "Z4": 0.0, "Z5": 0.0, "Z6": 0.0, "Z7": 1.0}


def test_wbal_min_depletes_above_cp() -> None:
    p = np.concatenate([np.full(600, 150.0), np.full(120, 350.0), np.full(600, 150.0)])
    wmin = power.wbal_min(p, cp=250.0, w_prime=20000.0)
    assert wmin is not None
    # 120 s at 100 W over CP: 12 kJ nominal, ~10.7 kJ after reconstitution with tau ~ 517 s
    # (DCP = 100 W), so about 9.3 kJ remains.
    assert 9000.0 < wmin < 10000.0
    assert power.wbal_min(p, cp=None, w_prime=20000.0) is None


def test_trainer_zero_exclusion_counts_stopped_seconds_as_moving() -> None:
    """Regression (real data, "include zeros" off): on a trainer, power *and* virtual speed are
    empty while not pedalling. Those seconds must stay moving at 0 W, as on intervals.icu."""
    import numpy as np

    from cyp.analysis.ride.frames import RideFrame
    from cyp.analysis.ride.power import moving_power, normalized_power
    from tests.analysis.conftest import make_ride

    n = 1200
    watts = np.full(n, 200.0)
    watts[::3] = np.nan  # every third second not pedalling
    speed = np.where(np.isnan(watts), np.nan, 8.0)
    import polars as pl

    df = make_ride(n, watts=np.nan_to_num(watts), hr=140.0, speed=np.nan_to_num(speed))
    gaps = pl.Series(np.isnan(watts))
    df = df.with_columns(
        pl.when(gaps).then(None).otherwise(pl.col("watts")).alias("watts"),
        pl.when(gaps).then(None).otherwise(pl.col("speed_mps")).alias("speed_mps"),
    )
    road = RideFrame.from_df(df)
    trainer = RideFrame.from_df(df, trainer=True)
    assert road.moving_s < 850 and trainer.moving_s == n
    np_trainer = normalized_power(moving_power(trainer))
    np_road = normalized_power(moving_power(road))
    assert np_trainer is not None and np_road is not None
    assert np_trainer < np_road  # zeros kept -> lower, honest NP


def test_short_rides_get_np() -> None:
    import numpy as np

    from cyp.analysis.ride.power import normalized_power

    assert normalized_power(np.full(300, 220.0)) == 220.0
    assert normalized_power(np.full(59, 220.0)) is None
