"""Dataset snapshot: content, cache hits/invalidation, and the vectorised PMC equivalence."""

from __future__ import annotations

import datetime as dt
import random

import pytest
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal import pmc
from cyp.core.data_quality import DataQuality
from cyp.dataset import DatasetCache, data_version, load_dataset
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    Athlete,
    AthleteSettingsHistory,
    WellnessDaily,
)

D = dt.date(2026, 9, 1)


def _seed(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.add(Athlete(id=1, intervals_id="i1", weight_kg=64.0))
        s.flush()
        s.add(
            AthleteSettingsHistory(
                athlete_id=1, effective_from=D, ftp=250.0, source="icu_sport_settings"
            )
        )
        s.add(
            AthleteSettingsHistory(
                athlete_id=1, effective_from=D + dt.timedelta(days=10), ftp=260.0, source="manual"
            )
        )
        s.add(
            Activity(
                id=10,
                athlete_id=1,
                sport_type="Ride",
                is_ride=True,
                start_utc="2026-09-02T23:00:00Z",
                start_local="2026-09-03T07:00:00",
                moving_s=3600,
                icu_training_load=80.0,
                raw_intervals_json={"big": "x" * 1000},
            )
        )
        s.add(
            ActivityMetrics(
                activity_id=10,
                algo_version="ride-1.1.0",
                tss=78.0,
                tss_source="power",
                power_curve={"watts": {"300": 300.0}},
                climbs=[{"fingerprint": "a"}],
                pacing={"classification": "vo2"},
                hr_drift_detail={"reliable": True},
            )
        )
        s.add(
            Activity(
                id=11,
                athlete_id=1,
                sport_type="Yoga",
                is_ride=False,
                start_utc="2026-09-03T12:00:00Z",
                start_local="2026-09-03T20:00:00",
            )
        )
        s.add(
            WellnessDaily(
                athlete_id=1,
                date_local=D,
                ctl=40.0,
                atl=45.0,
                hrv=60.0,
                raw_json={"sportInfo": [{"type": "Ride", "eftp": 262.0}]},
            )
        )
        s.commit()


def test_snapshot_content(factory: sessionmaker[Session]) -> None:
    _seed(factory)
    with factory() as s:
        ds = load_dataset(s)
    assert ds is not None and ds.athlete_id == 1
    ride, yoga = ds.activities
    assert ride.date == dt.date(2026, 9, 3) and ride.load == 80.0 and ride.load_source == "icu"
    assert ride.power_curve == {300: 300.0} and ride.classification == "vo2"
    assert ride.decoupling_reliable and ride.measured_power
    assert yoga.load == 0.0 and not yoga.analysed
    assert ds.loads[dt.date(2026, 9, 3)] == 80.0
    assert ds.ftp_on(D) == 250.0 and ds.ftp_on(D + dt.timedelta(days=30)) == 260.0
    assert ds.wellness[D].eftp == 262.0
    assert ds.ctl_atl(D) == (40.0, 45.0)
    # Past the last wellness day (no fitness rows yet): carry the latest known value.
    assert ds.ctl_atl(D + dt.timedelta(days=30)) == (40.0, 45.0)
    assert ds.ctl_atl(D - dt.timedelta(days=1)) == (0.0, 0.0)
    assert list(ds.rides()) == [ride]


def test_cache_reuses_until_data_changes(factory: sessionmaker[Session]) -> None:
    cache = DatasetCache()
    assert cache.get(factory) is None
    _seed(factory)
    first = cache.get(factory)
    again = cache.get(factory)
    assert first is again and cache.loads == 2  # empty + seeded
    with factory() as s:
        v1 = data_version(s)
        s.get(Activity, 10).icu_training_load = 90.0  # type: ignore[union-attr]
        s.commit()
        assert data_version(s) != v1
    changed = cache.get(factory)
    assert changed is not first and changed.loads[dt.date(2026, 9, 3)] == 90.0  # type: ignore[union-attr]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_vectorised_safety_matches_scalar(seed: int) -> None:
    rng = random.Random(seed)
    loads = {
        D + dt.timedelta(days=i): (0.0 if rng.random() < 0.3 else rng.uniform(20, 200))
        for i in range(120)
    }
    series = pmc.replay(
        pmc.PMCState(D + dt.timedelta(days=40), 50, 50), loads, D + dt.timedelta(days=119)
    )
    for day in series:
        assert day.acwr_7_28 == pytest.approx(pmc.acwr(loads, day.date))
        mono, strain = pmc.monotony_strain(loads, day.date)
        assert day.monotony_7 == pytest.approx(mono)
        assert day.strain_7 == pytest.approx(strain)


def test_daily_load_prefers_icu_ctl_load(factory: sessionmaker[Session]) -> None:
    """icu does not count e.g. strength/yoga towards CTL; its ctlLoad is the ledger."""
    _seed(factory)
    with factory() as s:
        s.add(WellnessDaily(athlete_id=1, date_local=dt.date(2026, 9, 3), ctl=41.0, ctl_load=70.0))
        s.commit()
        ds = load_dataset(s)
    assert ds is not None
    assert ds.loads[dt.date(2026, 9, 3)] == 70.0  # not the activity sum (80 + yoga)
    assert ds.activities[0].load == 80.0  # per-activity load unchanged


def test_power_fix_uses_our_tss_up_to_the_date(factory: sessionmaker[Session]) -> None:
    """Up to data_quality.power_zeros_excluded_until icu's load is not trusted."""
    _seed(factory)
    with factory() as s:
        s.add(WellnessDaily(athlete_id=1, date_local=dt.date(2026, 9, 3), ctl=41.0, ctl_load=70.0))
        s.commit()
    cache = DatasetCache()
    plain = cache.get(factory)
    fixed = cache.get(factory, data_quality=DataQuality(load_fix_until=dt.date(2026, 9, 5)))
    assert plain is not None and fixed is not None
    assert plain.loads[dt.date(2026, 9, 3)] == 70.0  # icu ledger
    assert fixed.loads[dt.date(2026, 9, 3)] == 78.0  # our TSS of ride 10 (+ yoga 0)
    assert fixed.power_fix_until == dt.date(2026, 9, 5)
    assert (
        cache.get(factory, data_quality=DataQuality(load_fix_until=dt.date(2026, 9, 5))) is fixed
    )  # memoised
    early = cache.get(factory, data_quality=DataQuality(load_fix_until=dt.date(2026, 9, 1)))
    assert early is not None and early.loads[dt.date(2026, 9, 3)] == 70.0
