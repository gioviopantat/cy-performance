"""Synthetic 1 Hz rides with hand-checkable values for the analysis tests."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import polars as pl
import pytest

from cyp.analysis.ride.durability import Thresholds
from cyp.analysis.ride.frames import RideFrame
from cyp.analysis.ride.power import coggan_zones
from cyp.analysis.ride.result import RideInputs
from cyp.core.athlete import Zone, ZoneModel
from cyp.store.streams import conform

FTP = 250.0
WEIGHT = 70.0
LTHR, MAX_HR, REST_HR = 160.0, 185.0, 50.0


def hr_zones() -> ZoneModel:
    bounds = [131, 146, 153, 163, 167, 172, 181]
    zones, lo = [], 0.0
    for i, b in enumerate(bounds):
        zones.append(Zone(idx=i + 1, name=f"Z{i + 1}", lo=lo, hi=float(b)))
        lo = float(b)
    return ZoneModel(kind="hr", anchor=LTHR, zones=zones)


def make_ride(
    n: int,
    *,
    watts: float | Sequence[float] | None = 200.0,
    hr: float | Sequence[float] | None = 140.0,
    speed: float | Sequence[float] = 8.0,
    alt: Sequence[float] | None = None,
    grade: Sequence[float] | None = None,
    latlng: tuple[float, float] | None = None,
    pauses: Sequence[tuple[int, int]] = (),
    stops: Sequence[tuple[int, int]] = (),
) -> pl.DataFrame:
    """1 Hz ride frame conforming to the Parquet schema.

    ``pauses`` are ``[start, end)`` second ranges with *no data* (device paused -> nulls);
    ``stops`` are ranges where the athlete stands still (speed 0, 0 W) but keeps recording.
    """
    t = np.arange(n)
    spd = np.full(n, float(speed)) if np.isscalar(speed) else np.asarray(speed, dtype=float)
    cols: dict[str, object] = {"t_s": t}
    w = None
    if watts is not None:
        w = np.full(n, float(watts)) if np.isscalar(watts) else np.asarray(watts, dtype=float)
    h = None
    if hr is not None:
        h = np.full(n, float(hr)) if np.isscalar(hr) else np.asarray(hr, dtype=float)
    for s, e in stops:
        spd[s:e] = 0.0
        if w is not None:
            w[s:e] = 0.0
    dist = np.cumsum(spd)
    a = np.asarray(alt, dtype=float) if alt is not None else np.full(n, 100.0)
    if grade is None:
        d_alt = np.gradient(a)
        g = np.where(spd > 0.1, d_alt / np.maximum(spd, 0.1) * 100.0, 0.0)
    else:
        g = np.asarray(grade, dtype=float)
    cols.update(
        {
            "dist_m": dist,
            "alt_m": a,
            "speed_mps": spd,
            "grade_pct": g,
        }
    )
    if w is not None:
        cols["watts"] = w
    if h is not None:
        cols["hr"] = h
    if latlng is not None:
        cols["lat"] = np.full(n, latlng[0]) + dist * 1e-6
        cols["lng"] = np.full(n, latlng[1])
    df = pl.DataFrame(cols)
    if pauses:
        mask = np.zeros(n, dtype=bool)
        for s, e in pauses:
            mask[s:e] = True
        paused = pl.Series("__p", mask)
        df = df.with_columns(
            [
                pl.when(paused).then(None).otherwise(pl.col(c)).alias(c)
                for c in df.columns
                if c != "t_s"
            ]
        )
    return conform(df)


def frame(df: pl.DataFrame) -> RideFrame:
    return RideFrame.from_df(df)


def inputs(**overrides: object) -> RideInputs:
    base = RideInputs(
        activity_id=1,
        has_power=True,
        has_hr=True,
        ftp=FTP,
        ftp_source="test",
        weight_kg=WEIGHT,
        lthr=LTHR,
        max_hr=MAX_HR,
        resting_hr=REST_HR,
        power_zones=coggan_zones(FTP),
        hr_zones=hr_zones(),
        zones_source="test",
        thresholds=Thresholds(),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


@pytest.fixture
def steady_ride() -> pl.DataFrame:
    """60 min at 200 W, HR 140, flat, 8 m/s: NP 200, IF 0.8, TSS 64, kJ 720, EF 1.4286."""
    return make_ride(3600)


@pytest.fixture
def drifting_ride() -> pl.DataFrame:
    """60 min at 200 W with HR drifting 130 -> 150 linearly (decoupling about 5.7 %)."""
    hr = 130.0 + 20.0 * np.arange(3600) / 3600.0
    return make_ride(3600, hr=hr)


@pytest.fixture
def interval_ride() -> pl.DataFrame:
    """10 min warm-up at 150 W then 4 x (5 min @ 290 W / 5 min @ 100 W), HR follows power."""
    n = 600 + 8 * 300
    w = np.full(n, 150.0)
    for k in range(4):
        s = 600 + k * 600
        w[s : s + 300] = 290.0
        w[s + 300 : s + 600] = 100.0
    hr = 110.0 + (w - 100.0) * 0.25
    return make_ride(n, watts=w, hr=hr)


@pytest.fixture
def climb_ride() -> pl.DataFrame:
    """20 min flat, 20 min climb at 8 % (8 m/s -> 768 m gain over 9.6 km), 20 min flat."""
    n = 3600
    third = 1200
    alt = np.full(n, 100.0)
    alt[third : 2 * third] = 100.0 + np.arange(third) * 8.0 * 0.08
    alt[2 * third :] = alt[2 * third - 1]
    return make_ride(n, alt=alt, latlng=(25.1, 121.5))


@pytest.fixture
def hr_only_ride() -> pl.DataFrame:
    """60 min flat at 8 m/s, HR 160 (= LTHR), no power: hrTSS 100, physics power about 134 W."""
    return make_ride(3600, watts=None, hr=160.0, grade=np.zeros(3600))
