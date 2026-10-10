"""climbs.py, efforts.py, estimate.py, classify.py."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from cyp.analysis.ride import climbs, efforts, estimate
from cyp.analysis.ride.classify import classify_ride
from cyp.analysis.ride.power import coggan_zones
from tests.analysis.conftest import FTP, LTHR, MAX_HR, REST_HR, WEIGHT, frame, make_ride

# ------------------------------------------------------------------------------- climbs


def test_detect_climb_hand_values(climb_ride: pl.DataFrame) -> None:
    found = climbs.detect_climbs(frame(climb_ride), weight_kg=WEIGHT)
    assert len(found) == 1
    c = found[0]
    # 1200 s at 8 m/s on 8 %: 9 600 m, 768 m gain, VAM = 768 / (1200/3600) = 2304 m/h.
    assert c["distance_m"] == pytest.approx(9600.0, rel=0.02)
    assert c["gain_m"] == pytest.approx(768.0, rel=0.02)
    assert c["avg_grade_pct"] == pytest.approx(8.0, abs=0.3)
    assert c["duration_s"] == pytest.approx(1200, abs=25)
    assert c["vam_m_h"] == pytest.approx(2304.0, rel=0.03)
    assert c["avg_w"] == pytest.approx(200.0) and c["w_kg"] == pytest.approx(
        200.0 / WEIGHT, abs=0.01
    )
    assert c["avg_hr"] == pytest.approx(140.0)
    assert sum(c["grade_bands_s"].values()) > 0 and c["grade_bands_s"]["6_9"] > 1000
    assert c["fingerprint"] is not None and c["fingerprint"].endswith("|L19")
    assert c["start_lat"] == pytest.approx(25.1, abs=0.02)


def test_detect_climbs_none_on_flat_and_long_grind(steady_ride: pl.DataFrame) -> None:
    assert climbs.detect_climbs(frame(steady_ride)) == []
    # 12 km / 215 m / 1.8 %: below the 3 % bar but a real climb via the grind path.
    n = 3000
    alt = 10.0 + np.arange(n) * (215.0 / n)
    found = climbs.detect_climbs(frame(make_ride(n, speed=4.0, alt=alt)))
    assert len(found) == 1
    assert 1.5 <= found[0]["avg_grade_pct"] < 3.0


def test_climb_not_split_by_small_dips() -> None:
    n = 3600
    third = 1200
    rng = np.random.default_rng(7)
    alt = np.empty(n)
    for i in range(n):
        if i < third:
            alt[i] = 100.0 + rng.uniform(-3, 3)
        elif i < 2 * third:
            dip = -6.0 if (i - third) % 120 < 8 else 0.0
            alt[i] = 100.0 + (i - third) * 8.0 * 0.08 + dip + rng.uniform(-3, 3)
        else:
            alt[i] = 100.0 + third * 8.0 * 0.08 + rng.uniform(-3, 3)
    found = climbs.detect_climbs(frame(make_ride(n, alt=alt)))
    assert len(found) == 1 and found[0]["gain_m"] > 100.0


def test_fingerprint_rounding() -> None:
    assert climbs.fingerprint(25.12345, 121.56789, 1200.0) == "25.123,121.568|L2"
    assert climbs.fingerprint(None, 121.5, 1200.0) is None


# ------------------------------------------------------------------------------- efforts


def test_detect_efforts_and_structure(interval_ride: pl.DataFrame) -> None:
    fr = frame(interval_ride)
    zones = coggan_zones(FTP)
    found = efforts.detect_efforts(fr, ftp=FTP, weight_kg=WEIGHT, zones=zones)
    assert len(found) == 4
    assert all(e["label"] == "vo2" and e["zone"] == 5 for e in found)  # 290 W = 116 % FTP
    assert all(abs(e["duration_s"] - 300) <= 30 for e in found)
    assert found[0]["avg_w"] == pytest.approx(290.0, abs=5)
    structure = efforts.effort_structure(found)
    assert structure["count"] == 4 and structure["regular"] is True
    assert structure["mean_recovery_s"] == pytest.approx(300, abs=30)
    assert efforts.detect_sprints(fr, ftp=FTP) == []  # 290 W < 1.2 * 250 and < 1.5 * mean


def test_detect_sprints() -> None:
    w = np.full(600, 100.0)
    w[200:230] = 500.0
    fr = frame(make_ride(600, watts=w))
    sprints = efforts.detect_sprints(fr, ftp=FTP, weight_kg=WEIGHT)
    assert len(sprints) == 1
    assert sprints[0]["kind"] == "sprint" and sprints[0]["duration_s"] == 30
    assert sprints[0]["start_s"] == 200
    # Stop-start micro-surges to 260 W must not count for a 275 W-FTP rider.
    w2 = np.zeros(1200)
    for s in range(0, 1200, 40):
        w2[s : s + 10] = 260.0
    assert efforts.detect_sprints(frame(make_ride(1200, watts=w2)), ftp=275.0) == []


# ------------------------------------------------------------------------------- estimate


def test_physics_power_flat_hand_value(hr_only_ride: pl.DataFrame) -> None:
    # Flat, 8 m/s, 78 kg total: (0.005*78*g + 0.5*1.225*0.32*64) * 8 / 0.975 = 134.3 W
    fr = frame(hr_only_ride)
    p = estimate.estimate_power_series(fr, WEIGHT)
    assert p is not None
    assert float(np.nanmedian(p)) == pytest.approx(134.3, abs=1.5)
    assert estimate.estimate_power_series(fr, None) is None


def test_hr_tss_is_100_per_hour_at_lthr(hr_only_ride: pl.DataFrame) -> None:
    fr = frame(hr_only_ride)
    value, method = estimate.hr_tss(fr, lthr=LTHR, max_hr=MAX_HR, resting_hr=REST_HR)
    assert value == pytest.approx(100.0, abs=0.5)
    assert "估算" in method
    assert estimate.hr_tss(fr, lthr=None, max_hr=MAX_HR, resting_hr=REST_HR)[0] is None
    fr_no_hr = frame(make_ride(600, watts=None, hr=None))
    assert estimate.hr_tss(fr_no_hr, lthr=LTHR, max_hr=MAX_HR, resting_hr=REST_HR)[0] is None


def test_estimate_for_ride_bundle(hr_only_ride: pl.DataFrame) -> None:
    res = estimate.estimate_for_ride(
        frame(hr_only_ride), weight_kg=WEIGHT, lthr=LTHR, max_hr=MAX_HR, resting_hr=REST_HR
    )
    assert res.power_est is not None and res.hr_tss == pytest.approx(100.0, abs=0.5)
    meta = res.to_meta()
    assert meta["power_estimated"] is True and set(meta["methods"]) == {"power", "hr_tss"}  # type: ignore[arg-type]


# ------------------------------------------------------------------------------- classify


@pytest.mark.parametrize(
    ("tiz", "if_", "moving", "race", "expected"),
    [
        (None, None, 3600, True, "race"),
        ({"Z1": 500, "Z2": 2500, "Z3": 300, "Z4": 200, "Z5": 100}, 0.7, 3600, False, "endurance"),
        ({"Z1": 3000, "Z2": 500}, 0.5, 3600, False, "recovery"),
        ({"Z1": 1000, "Z2": 1000, "Z3": 1500, "Z4": 100}, 0.8, 3600, False, "tempo"),
        ({"Z1": 1000, "Z2": 1000, "Z3": 1000, "Z4": 800}, 0.88, 3600, False, "sweetspot"),
        ({"Z1": 1500, "Z2": 700, "Z3": 300, "Z4": 1100}, 0.9, 3600, False, "threshold"),
        (
            {"Z1": 2000, "Z2": 800, "Z3": 100, "Z4": 100, "Z5": 400, "Z6": 300},
            0.9,
            3600,
            False,
            "vo2",
        ),
        (
            {"Z1": 2000, "Z2": 800, "Z3": 100, "Z4": 100, "Z5": 300},
            0.77,
            3600,
            False,
            "tempo",
        ),  # hilly Z2
        ({"Z1": 1000, "Z2": 1000, "Z3": 1000, "Z4": 500}, 0.82, 3600, False, "tempo"),
        (None, None, 3600, False, "mixed"),
        ({"Z1": 1200, "Z2": 1200, "Z3": 1200}, None, 3600, False, "tempo"),
    ],
)
def test_classify_ride(tiz, if_, moving, race, expected) -> None:  # type: ignore[no-untyped-def]
    assert classify_ride(tiz, if_, moving_s=moving, race=race) == expected
