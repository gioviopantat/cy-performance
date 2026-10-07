"""Deterministic synthetic athlete for benchmarks, demos and frontend development.

``seed_synthetic(factory, store, days=400)`` fills an empty store with a plausible season:

- one athlete (70 kg) whose FTP drifts 245 -> 265 W over the period (settings history every
  8 weeks, icu-style eFTP in wellness ``sportInfo``);
- ~5 rides per week following a fixed weekly pattern (Tue sweet spot / threshold, Thu VO2,
  Sat long ride with two repeats of a "home climb", Sun endurance, Wed/Fri Z2) plus a
  strength session on Mondays, with random skips;
- 1 Hz streams (power, HR with a first-order lag and kJ-dependent drift, cadence, speed,
  altitude, grade, lat/lng — the home climb always starts at the same coordinates so climb
  fingerprints repeat);
- wellness every day (HRV, resting HR, sleep, subjective scores) and icu CTL/ATL computed from
  the same daily loads with icu's exponential update.

Per-ride metrics are produced by the real analysis pipeline, so every downstream model sees
exactly what it would see on live data. Same ``seed`` -> same database.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import numpy as np
import polars as pl
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.run import analyze_pending
from cyp.store.models import Activity, ActivityMetrics, Athlete, WellnessDaily
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.streams import StreamStore

WEIGHT_KG = 70.0
HOME = (25.0330, 121.5654)
CLIMB_START = (25.1000, 121.5300)
K42, K7 = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)


@dataclass(frozen=True)
class SyntheticSummary:
    """What :func:`seed_synthetic` created."""

    days: int
    rides: int
    other: int
    first_day: dt.date
    last_day: dt.date


def _ftp_on(day: dt.date, start: dt.date, days: int) -> float:
    return 245.0 + 20.0 * (day - start).days / max(days - 1, 1)


def _segments(kind: str, ftp: float, rng: np.random.Generator) -> list[tuple[int, float]]:
    """``(seconds, watts)`` blocks for a ride type."""
    z2 = 0.66 * ftp
    if kind == "ss":
        reps, work = int(rng.integers(3, 5)), int(rng.choice([600, 720, 900]))
        body = [(work, 0.9 * ftp), (240, 0.55 * ftp)] * reps
        return [(900, 0.6 * ftp), *body, (1200, z2)]
    if kind == "vo2":
        body = [(300, 1.12 * ftp), (300, 0.5 * ftp)] * int(rng.integers(4, 6))
        return [(1200, 0.62 * ftp), *body, (900, z2)]
    if kind == "threshold":
        return [
            (900, 0.6 * ftp),
            (1200, 0.98 * ftp),
            (480, 0.55 * ftp),
            (1200, 0.97 * ftp),
            (900, z2),
        ]
    if kind == "long":
        n = int(rng.choice([10800, 12600, 14400]))
        climb = (900, 0.88 * ftp)
        flat = (n - 2 * 900 - 1800) // 3
        return [
            (1200, 0.6 * ftp),
            (flat, z2),
            climb,
            (600, 0.45 * ftp),
            (flat, z2),
            climb,
            (flat, 0.7 * ftp),
        ]
    minutes = int(rng.choice([60, 75, 90, 120]))
    return [(minutes * 60, z2 * float(rng.uniform(0.95, 1.05)))]


def _stream(
    segments: list[tuple[int, float]], ftp: float, rng: np.random.Generator, *, climbs: bool
) -> pl.DataFrame:
    watts = np.concatenate([np.full(n, w) for n, w in segments])
    n = watts.size
    watts = np.clip(watts * (1 + rng.normal(0, 0.06, n)), 0, None)
    watts[rng.random(n) < 0.03] = 0.0  # coasting
    kj = np.cumsum(watts) / 1000
    target = 52 + 120 * (watts / ftp) + 0.004 * kj  # cardiac drift with work
    hr = np.empty(n)
    hr[0] = 95.0
    alpha = 1 / 30
    for i in range(1, n):  # first-order lag, tau 30 s
        hr[i] = hr[i - 1] + alpha * (target[i] - hr[i - 1])
    grade = np.zeros(n)
    if climbs:
        t0 = 0
        for seg_n, w in segments:
            if abs(w - 0.88 * ftp) < 1e-6:
                grade[t0 : t0 + seg_n] = 6.0
            t0 += seg_n
    speed = np.clip(9.0 * (watts / 200) ** (1 / 3) - grade * 0.9, 2.0, 15.0)
    speed[watts == 0] = np.maximum(speed[watts == 0] * 0.8, 2.0)
    dist = np.cumsum(speed)
    alt = 20 + np.cumsum(speed * grade / 100)
    lat = np.full(n, HOME[0]) + dist * 4e-6
    lng = np.full(n, HOME[1]) + dist * 2e-6
    if climbs:
        t0 = 0
        for seg_n, w in segments:
            if abs(w - 0.88 * ftp) < 1e-6:
                seg = slice(t0, t0 + seg_n)
                lat[seg] = CLIMB_START[0] + (dist[seg] - dist[t0]) * 5e-6
                lng[seg] = CLIMB_START[1]
            t0 += seg_n
    return pl.DataFrame(
        {
            "t_s": np.arange(n),
            "watts": np.round(watts),
            "hr": np.round(hr),
            "cad": np.where(watts > 0, 88, 0),
            "speed_mps": speed,
            "dist_m": dist,
            "alt_m": alt,
            "grade_pct": grade,
            "lat": lat,
            "lng": lng,
            "moving": np.ones(n, dtype=bool),
        }
    )


def seed_synthetic(
    factory: sessionmaker[Session],
    store: StreamStore,
    *,
    days: int = 400,
    end: dt.date | None = None,
    seed: int = 7,
    analyze: bool = True,
) -> SyntheticSummary:
    """Populate an *empty* store (see module docstring).

    Raises:
        ValueError: the store already has an athlete.
    """
    rng = np.random.default_rng(seed)
    last = end or dt.date.today() - dt.timedelta(days=1)
    first = last - dt.timedelta(days=days - 1)
    pattern = {1: "ss", 2: "z2", 3: "vo2", 4: "z2", 5: "long", 6: "z2"}
    n_rides = n_other = 0
    with factory() as s:
        if s.query(Athlete).count():
            raise ValueError("store is not empty; synthetic data needs a fresh database")
        s.add(
            Athlete(
                id=1,
                intervals_id="i000000",
                name="Synthetic Rider",
                weight_kg=WEIGHT_KG,
                timezone="Asia/Taipei",
            )
        )
        s.flush()
        repo = AthleteSettingsRepo(s)
        for i in range(0, days, 56):
            day = first + dt.timedelta(days=i)
            ftp_w = round(_ftp_on(day, first, days))
            repo.append_if_changed(
                1,
                day,
                {
                    "ftp": float(ftp_w),
                    "lthr": 168,
                    "max_hr": 190,
                    "resting_hr": 46,
                    "weight_kg": WEIGHT_KG,
                    "power_zones": None,
                    "hr_zones": None,
                },
                source="icu_sport_settings",
            )
        acts = ActivityRepo(s)
        for i in range(days):
            day = first + dt.timedelta(days=i)
            kind = pattern.get(day.weekday())
            if day.weekday() == 0:
                s.add(
                    Activity(
                        athlete_id=1,
                        sport_type="WeightTraining",
                        is_ride=False,
                        name="重訓",
                        start_utc=f"{day.isoformat()}T11:00:00Z",
                        start_local=f"{day.isoformat()}T19:00:00",
                        tz="Asia/Taipei",
                        moving_s=3600,
                        elapsed_s=3600,
                        icu_training_load=35.0,
                        pending_analysis=False,
                    )
                )
                n_other += 1
                continue
            if kind is None or rng.random() < 0.12:
                continue
            if kind == "ss" and (i // 7) % 3 == 2:
                kind = "threshold"
            ftp = _ftp_on(day, first, days)
            frame = _stream(_segments(kind, ftp, rng), ftp, rng, climbs=kind == "long")
            a = Activity(
                athlete_id=1,
                sport_type="Ride",
                is_ride=True,
                intervals_id=f"isyn{i}",
                name={
                    "ss": "甜蜜點",
                    "vo2": "VO2 間歇",
                    "threshold": "閾值 2x20",
                    "long": "週末長騎",
                    "z2": "Z2 耐力",
                }[kind],
                start_utc=f"{day.isoformat()}T22:30:00Z",
                start_local=f"{(day + dt.timedelta(days=1)).isoformat()}T06:30:00",
                tz="Asia/Taipei",
                moving_s=frame.height,
                elapsed_s=frame.height,
                distance_m=float(frame["dist_m"][-1]),
                has_power=True,
                has_hr=True,
                has_cadence=True,
                kj=float(frame["watts"].to_numpy().sum() / 1000),
                avg_w=float(frame["watts"].to_numpy().mean()),
                avg_hr=float(frame["hr"].to_numpy().mean()),
                icu_ftp=round(ftp),
                trainer=False,
                raw_intervals_json={"icu_power_zones": [55, 75, 90, 105, 120, 150, 999]},
            )
            s.add(a)
            s.flush()
            path = store.write(a.id, frame)
            acts.upsert_stream_file(
                a.id,
                {
                    "source": "intervals",
                    "path": str(path),
                    "columns": frame.columns,
                    "hz": 1.0,
                    "n_samples": frame.height,
                    "resolution": "1s",
                },
            )
            a.pending_streams = False
            n_rides += 1
        s.commit()
    if analyze:
        analyze_pending(factory, store=store)
    _fill_loads_and_wellness(factory, first, last, rng)
    return SyntheticSummary(days, n_rides, n_other, first, last)


def _fill_loads_and_wellness(
    factory: sessionmaker[Session], first: dt.date, last: dt.date, rng: np.random.Generator
) -> None:
    from cyp.analysis.run import activity_local_date

    with factory() as s:
        loads: dict[dt.date, float] = {}
        for a, tss in s.query(Activity, ActivityMetrics.tss).outerjoin(
            ActivityMetrics, ActivityMetrics.activity_id == Activity.id
        ):
            if a.icu_training_load is None and tss is not None:
                a.icu_training_load = round(float(tss), 1)
            d = activity_local_date(a)
            loads[d] = loads.get(d, 0.0) + float(a.icu_training_load or 0.0)
        ctl, atl = 40.0, 40.0
        n = (last - first).days + 1
        for i in range(n):
            day = first + dt.timedelta(days=i)
            load = loads.get(day, 0.0)
            ctl += (load - ctl) * K42
            atl += (load - atl) * K7
            tsb = ctl - atl
            eftp = _ftp_on(day, first, n) * 1.02 + float(rng.normal(0, 2))
            s.add(
                WellnessDaily(
                    athlete_id=1,
                    date_local=day,
                    ctl=round(ctl, 2),
                    atl=round(atl, 2),
                    hrv=round(float(62 + 0.25 * tsb + rng.normal(0, 4)), 1),
                    resting_hr=round(float(47 - 0.08 * tsb + rng.normal(0, 1.5)), 1),
                    sleep_s=int(7 * 3600 + rng.normal(0, 2400)),
                    sleep_score=round(float(np.clip(78 + rng.normal(0, 8), 30, 100)), 0),
                    soreness=int(np.clip(round(1.8 - tsb / 25 + rng.normal(0, 0.6)), 1, 4)),
                    fatigue=int(np.clip(round(2.0 - tsb / 20 + rng.normal(0, 0.6)), 1, 4)),
                    stress=int(np.clip(round(2 + rng.normal(0, 0.7)), 1, 4)),
                    mood=int(np.clip(round(2 + rng.normal(0, 0.7)), 1, 4)),
                    weight_kg=round(WEIGHT_KG + float(rng.normal(0, 0.4)), 1),
                    raw_json={"sportInfo": [{"type": "Ride", "eftp": round(eftp, 1)}]},
                )
            )
        s.commit()
