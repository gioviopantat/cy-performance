"""Durability vs kJ, TID / polarization, repeat-climb leaderboard, limiters."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from cyp.analysis.longitudinal import climbs, durability, limiters, tid
from cyp.analysis.ride.frames import RideFrame
from tests.analysis.conftest import make_ride

DAY = dt.date(2026, 9, 20)


def _long_ride(drift_after_kj: float | None) -> RideFrame:
    """4 h at 180 W (≈ 2 590 kJ); HR 130, rising 8 % once ``drift_after_kj`` is passed."""
    n = 4 * 3600
    watts = np.full(n, 180.0)
    kj = np.cumsum(watts) / 1000
    hr = np.full(n, 130.0)
    if drift_after_kj is not None:
        hr = np.where(kj >= drift_after_kj, 130.0 * 1.08, 130.0)
    return RideFrame.from_df(make_ride(n, watts=watts, hr=hr))


def test_ef_by_kj_flat_ride_has_ratio_one() -> None:
    r = durability.ef_by_kj(_long_ride(None), 260.0, activity_id=1, date=DAY)
    assert r is not None
    assert r.total_kj == pytest.approx(2592, abs=1)
    assert set(r.ef_by_bucket) == {"0-500", "500-1000", "1000-1500", "1500-2000", "2000-2500"}
    assert r.ratio == pytest.approx(1.0)
    assert r.ef_fresh == pytest.approx(180 / 130, rel=1e-3)


def test_ef_by_kj_detects_fade() -> None:
    r = durability.ef_by_kj(_long_ride(1000.0), 260.0, activity_id=1, date=DAY)
    # HR is stored as int16: 130 * 1.08 = 140.4 -> 140 bpm.
    assert r is not None and r.ratio == pytest.approx(130 / 140, rel=1e-3)


def test_ef_by_kj_ignores_out_of_band_and_missing_streams() -> None:
    # 180 W vs FTP 150 = 120 % -> outside the endurance band: no buckets.
    r = durability.ef_by_kj(_long_ride(None), 150.0, activity_id=1, date=DAY)
    assert r is not None and r.ef_by_bucket == {} and r.ratio is None
    no_hr = RideFrame.from_df(make_ride(600, watts=180.0, hr=None))
    assert durability.ef_by_kj(no_hr, 260.0, activity_id=1, date=DAY) is None


def test_durability_trend_blocks() -> None:
    rides = [
        durability.RideDurability(1, DAY, 1500, {"0-500": 1.40, "1000-1500": 1.33}),
        durability.RideDurability(2, DAY - dt.timedelta(days=3), 900, {"0-500": 1.4}),
        durability.RideDurability(
            3, DAY - dt.timedelta(days=40), 1600, {"0-500": 1.4, "1500-2000": 1.26}
        ),
    ]
    blocks = durability.durability_trend(rides, end=DAY, n_blocks=2)
    assert [b.n_rides for b in blocks] == [1, 2]
    assert blocks[-1].median_ratio == pytest.approx(0.95)
    assert blocks[0].median_ratio == pytest.approx(0.9)
    e = durability.explain_durability(blocks, as_of=DAY)
    assert "5.0 %" in e.headline_zh and "進步" in e.because[1].text_zh
    empty = durability.explain_durability(durability.durability_trend([], end=DAY), as_of=DAY)
    assert empty.confidence == "low"


def test_weekly_tid_and_polarization() -> None:
    mon = dt.date(2026, 9, 14)
    rides = [
        tid.RideTiz(mon, {"Z1": 1000, "Z2": 31400, "Z3": 4000, "Z4": 2000, "Z5": 2100}),
        tid.RideTiz(mon + dt.timedelta(days=6), {"Z2": 0}),
        tid.RideTiz(mon + dt.timedelta(days=7), {"Z3": 3600}),
    ]
    weeks = tid.weekly_tid(rides)
    assert [w.week_start for w in weeks] == [mon, mon + dt.timedelta(days=7)]
    w = weeks[0]
    low, mid, high = w.fractions()
    assert (round(low, 2), round(mid, 2), round(high, 2)) == (0.8, 0.15, 0.05)
    assert w.polarization_index == pytest.approx(math.log10((low / mid) * high * 100))
    assert w.model == "pyramidal"
    assert weeks[1].model == "threshold" and weeks[1].polarization_index is None
    no_mid = tid.weekly_tid([tid.RideTiz(mon, {"Z2": 5000, "Z5": 4000})])[0]
    assert no_mid.model == "polarized" and no_mid.polarization_index is None
    e = tid.explain_week(w, "base")
    assert e.key == "tid.2026-09-14" and "base 期目標" in e.because[-1].text_zh


def test_tiz_source_priority() -> None:
    assert tid.tiz_from_metrics({"Z1": 5}, {"Z1": 9}, [1, 2]) == ({"Z1": 5.0}, "power")
    assert tid.tiz_from_metrics(None, {"Z1": 9}, [1, 2]) == ({"Z1": 1.0, "Z2": 2.0}, "power_icu")
    assert tid.tiz_from_metrics(None, {"Z1": 9}, None) == ({"Z1": 9.0}, "hr")
    assert tid.tiz_from_metrics(None, None, []) is None


def test_climb_leaderboard() -> None:
    fp = "25.100,121.500|L6"

    def c(dur: int, wkg: float) -> dict[str, object]:
        return {
            "fingerprint": fp,
            "duration_s": dur,
            "w_kg": wkg,
            "distance_m": 3200,
            "gain_m": 210,
        }

    rides = [
        (1, dt.date(2026, 7, 1), [c(900, 3.6)]),
        (2, dt.date(2026, 8, 1), [c(860, 3.8), {"fingerprint": None, "duration_s": 10}]),
        (3, dt.date(2026, 9, 1), [c(880, 3.9), {"fingerprint": "other", "duration_s": 300}]),
        (4, dt.date(2026, 9, 2), "not-a-list"),
    ]
    boards = climbs.leaderboard(rides)
    assert len(boards) == 1
    b = boards[0]
    assert b.best.activity_id == 2 and b.latest.activity_id == 3 and b.latest_rank == 2
    assert b.wkg_slope_per_30d is not None and b.wkg_slope_per_30d > 0
    assert b.distance_m == 3200 and b.gain_m == 210


def test_limiters_rules_and_bias() -> None:
    lims = limiters.detect_limiters(
        ftp=265, mmp={300: 290.0, 1200: 268.0}, durability_ratio=0.93, mid_share_4w=0.35, ctl=45
    )
    ids = [lim.id for lim in lims]
    assert set(ids) == {
        "vo2_ceiling",
        "sustained_power",
        "durability",
        "grey_zone",
        "aerobic_volume",
    }
    assert ids == sorted(ids, key=lambda i: -next(x.severity for x in lims if x.id == i))
    assert all(lim.explanation is not None for lim in lims)
    bias = limiters.combined_bias(lims)
    assert bias["vo2"] > 1 and bias["endurance"] > 1
    healthy = limiters.detect_limiters(
        ftp=265, mmp={300: 320.0, 1200: 280.0}, durability_ratio=0.98, mid_share_4w=0.15, ctl=70
    )
    assert healthy == [] and limiters.combined_bias(healthy) == {}
