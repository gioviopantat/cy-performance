"""An invented athlete's store for the per-ride report tests (nothing here is real data).

FTP 250 W, 70 kg. Wellness carries a steadily rising icu eFTP (so the goal projection has a
trend) and a low CTL (so the ``aerobic_volume`` limiter fires). Four earlier rides climb the
same 2.2 km / 6 % road as the report ride, in cooler weather. The report ride
(:data:`RIDE_DAY`) is an outdoor sweet-spot 3x10 in 31 °C heat with a red light inside rep 2
and eight more stops on the way home; an indoor ride the day before has no plan.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import polars as pl
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.run import analyze_pending
from cyp.planning.templates import load_library, resolve
from cyp.store.models import Activity, Athlete, PlannedWorkout, WellnessDaily
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.streams import StreamStore, conform

FTP = 250.0
WEIGHT = 70.0
RIDE_DAY = dt.date(2026, 10, 14)
INDOOR_DAY = dt.date(2026, 10, 13)
HISTORY_DAYS = (
    dt.date(2026, 9, 16),
    dt.date(2026, 9, 23),
    dt.date(2026, 9, 30),
    dt.date(2026, 10, 7),
)
HISTORY_CLIMB_W = (210.0, 230.0, 220.0, 240.0)
#: Base position of every outdoor ride (invented, at sea).
LAT0, LNG0 = 10.0, 100.0
CLIMB_M = 2200.0
SS_PARAMS = {"reps": 3, "work_min": 10, "pct": 88}
CLIMB_GRADE = 6.0


@dataclass(frozen=True)
class Seg:
    """One stretch of a synthetic ride."""

    seconds: int
    watts: float
    speed: float = 8.0
    grade: float = 0.0
    stop: bool = False


def build_stream(
    segs: Sequence[Seg], *, temp_c: float | None = None, hr0: float = 110.0
) -> pl.DataFrame:
    """1 Hz frame from segments (stops: speed 0, power 0, still recording)."""
    w, v, g = [], [], []
    for s in segs:
        w += [0.0 if s.stop else s.watts] * s.seconds
        v += [0.0 if s.stop else s.speed] * s.seconds
        g += [0.0 if s.stop else s.grade] * s.seconds
    watts, speed, grade = np.array(w), np.array(v), np.array(g)
    n = watts.size
    dist = np.cumsum(speed)
    alt = 50.0 + np.cumsum(speed * grade / 100.0)
    cols: dict[str, object] = {
        "t_s": np.arange(n),
        "dist_m": dist,
        "lat": LAT0 + dist * 1e-6,
        "lng": np.full(n, LNG0),
        "alt_m": alt,
        "speed_mps": speed,
        "grade_pct": grade,
        "watts": watts,
        "hr": hr0 + np.maximum(watts - 100.0, 0.0) * 0.25,
        "cad": np.where(watts > 0, 88, 0),
    }
    if temp_c is not None:
        cols["temp_c"] = np.full(n, temp_c) + np.where(np.arange(n) > n // 2, 2.0, 0.0)
    return conform(pl.DataFrame(cols))


def warmup_and_climb(climb_w: float, climb_speed: float = 4.0) -> list[Seg]:
    """15 min warm-up on the flat, then the 2.2 km / 6 % climb (same start for every ride)."""
    return [
        Seg(900, 150.0),
        Seg(round(CLIMB_M / climb_speed), climb_w, climb_speed, CLIMB_GRADE),
    ]


def report_ride_segments() -> list[Seg]:
    """Sweet spot 3x10 at 225 W: rep 1 on the climb, rep 2 with a 45 s red light, then home."""
    segs = warmup_and_climb(225.0, 3.7)  # 595 s at 225 W
    segs += [Seg(240, 0.0, 10.0, -CLIMB_GRADE)]  # descent, coasting
    segs += [Seg(260, 225.0), Seg(45, 0.0, stop=True), Seg(300, 225.0)]  # rep 2, red light
    segs += [Seg(240, 130.0)]
    segs += [Seg(600, 225.0)]  # rep 3
    for _ in range(8):  # ride home through town
        segs += [Seg(420, 160.0), Seg(40, 0.0, stop=True)]
    segs += [Seg(300, 140.0)]
    return segs


def _activity(
    s: Session,
    store: StreamStore,
    day: dt.date,
    idx: int,
    frame: pl.DataFrame,
    *,
    indoor: bool = False,
    **kw: object,
) -> Activity:
    watts = frame["watts"].to_numpy()
    a = Activity(
        athlete_id=1,
        sport_type="VirtualRide" if indoor else "Ride",
        is_ride=True,
        intervals_id=f"iw{idx}",
        name=kw.pop("name", f"ride {idx}"),
        start_utc=f"{(day - dt.timedelta(days=1)).isoformat()}T23:00:00Z",
        start_local=f"{day.isoformat()}T07:00:00",
        tz="Asia/Taipei",
        moving_s=int((frame["speed_mps"].to_numpy() > 0.5).sum()),
        elapsed_s=frame.height,
        distance_m=float(frame["dist_m"][-1]),
        elev_gain_m=float(max(frame["alt_m"].to_numpy()) - 50.0),
        trainer=indoor,
        has_power=True,
        has_hr=True,
        has_cadence=True,
        kj=float(watts.sum() / 1000),
        avg_w=float(watts.mean()),
        raw_intervals_json={"icu_power_zones": [55, 75, 90, 105, 120, 150, 999]},
        **kw,
    )
    s.add(a)
    s.flush()
    path = store.write(a.id, frame)
    ActivityRepo(s).upsert_stream_file(
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
    return a


def seed_world(
    factory: sessionmaker[Session],
    store: StreamStore,
    *,
    eftp_days: int = 70,
    rpe: int | None = 7,
    feel: int | None = 2,
) -> dict[str, int]:
    """Populate an empty store; returns ``{"ride": id, "indoor": id}``.

    ``eftp_days`` limits the wellness eFTP series to the last N days (few days -> no goal date).
    """
    with factory() as s:
        s.add(Athlete(id=1, intervals_id="i999", name="Test Rider", weight_kg=WEIGHT))
        s.flush()
        AthleteSettingsRepo(s).append_if_changed(
            1,
            dt.date(2026, 6, 1),
            {
                "ftp": FTP,
                "lthr": 165,
                "max_hr": 190,
                "resting_hr": 48,
                "weight_kg": WEIGHT,
                "power_zones": None,
                "hr_zones": None,
            },
            source="icu_sport_settings",
        )
        for i in range(70):
            day = RIDE_DAY - dt.timedelta(days=69 - i)
            eftp = 240.0 + 0.3 * i if i >= 70 - eftp_days else None
            s.add(
                WellnessDaily(
                    athlete_id=1,
                    date_local=day,
                    ctl=40.0,
                    atl=45.0,
                    raw_json={"sportInfo": [{"type": "Ride", "eftp": eftp}]} if eftp else None,
                )
            )
        for k, (day, w) in enumerate(zip(HISTORY_DAYS, HISTORY_CLIMB_W, strict=True)):
            segs = [*warmup_and_climb(w, 3.6 + 0.1 * k), Seg(240, 0.0, 10.0, -CLIMB_GRADE)]
            segs += [Seg(2400, 170.0)]
            _activity(s, store, day, k, build_stream(segs, temp_c=22.0))
        indoor = build_stream([Seg(600, 140.0), Seg(2400, 180.0), Seg(600, 130.0)], hr0=105.0)
        indoor_a = _activity(s, store, INDOOR_DAY, 10, indoor, indoor=True, name="indoor z2")
        frame = build_stream(report_ride_segments(), temp_c=31.0)
        ride = _activity(s, store, RIDE_DAY, 11, frame, name="ss 3x10", icu_rpe=rpe, feel=feel)
        s.add(
            PlannedWorkout(
                athlete_id=1,
                date_local=RIDE_DAY,
                slot=1,
                external_id="cyp:test:ss",
                template_id="ss_3x_n",
                template_version="1",
                name=resolve(load_library()["ss_3x_n"], dict(SS_PARAMS), outdoor=True).name_zh,
                intent="sweetspot",
                steps={"params": dict(SS_PARAMS), "role": "hit"},
                target_tss=75.0,
                target_duration_s=4200,
                indoor=False,
                status="proposed",
            )
        )
        ids = {"ride": ride.id, "indoor": indoor_a.id}
        s.commit()
    analyze_pending(factory, store=store)
    return ids
