"""An invented athlete's autumn for the digest tests (nothing here is real data).

FTP 250 W, 70 kg, the reference athlete.yaml (season from 2026-10-05, 3:1, base phase; week 4,
2026-10-26, is the recovery / ramp-test week). Wellness carries HRV, resting HR, sleep and a
rising icu eFTP from July; CTL stays low, so the ``aerobic_volume`` limiter fires.

- September: four rides up the same 2.2 km / 6 % climb in 22 °C plus one long ride (the
  durability baseline and "last month" of the power curve).
- Week A, 2026-10-05 (:data:`WEEK_FULL`), indoor but Sunday: sweet spot achieved (Tue), VO2
  ridden far under the band (Wed, missed), a recovery day ridden for 90 min (Thu, overdone),
  an endurance day not ridden (Fri, skipped), a long endurance ride with HR drift (Sat,
  achieved, durability) and an unplanned outdoor Sunday ride: the climb's best time inside
  20 min at 105 % FTP, then 5 and 10 min efforts (FTP evidence, CP fit).
- Week B, 2026-10-12 (:data:`WEEK_OUTDOOR`), outdoor: the ride report's sweet spot 3x10 with
  nine stops in 31 °C (Tue), an endurance ride with town stops (Thu), a long ride (Sat).
- Week C, 2026-10-19 (:data:`WEEK_BREAK`): a trip (``availability.dates`` 0, set by
  :func:`trip_config`); two sessions planned before the trip was entered, no ride.
- Week D, 2026-10-26: the recovery week, two easy rides; one more ride on 2026-11-01.
- Stored plans for 2026-11-03 / 11-05 (the next month). December has nothing.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence

import polars as pl
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal.run import build_trends
from cyp.analysis.readiness_job import run_readiness
from cyp.analysis.run import analyze_pending
from cyp.planning.templates import load_library, resolve
from cyp.settings import AthleteConfig
from cyp.store.models import Athlete, PlannedWorkout, WellnessDaily
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.streams import StreamStore
from tests.reports.world import (
    CLIMB_GRADE,
    FTP,
    WEIGHT,
    Seg,
    _activity,
    build_stream,
    report_ride_segments,
    warmup_and_climb,
)

WEEK_FULL = dt.date(2026, 10, 5)
WEEK_OUTDOOR = dt.date(2026, 10, 12)
WEEK_BREAK = dt.date(2026, 10, 19)
WEEK_RECOVERY = dt.date(2026, 10, 26)
MONTH = dt.date(2026, 10, 1)
EMPTY_MONTH = dt.date(2026, 12, 1)
TRIP = (dt.date(2026, 10, 19), dt.date(2026, 10, 25))
WELLNESS_FROM = dt.date(2026, 7, 1)
WELLNESS_TO = dt.date(2026, 11, 2)
#: Days the 7-day HRV runs low (a poor week of sleep inside week B).
HRV_DIP = (dt.date(2026, 10, 13), dt.date(2026, 10, 17))


def trip_config(cfg: AthleteConfig) -> AthleteConfig:
    """The reference config with the week-C trip as 0 available minutes."""
    days = {TRIP[0] + dt.timedelta(days=i): 0 for i in range((TRIP[1] - TRIP[0]).days + 1)}
    availability = cfg.availability.model_copy(update={"dates": days})
    return cfg.model_copy(update={"availability": availability})


def _drift(frame: pl.DataFrame, bpm_per_h: float) -> pl.DataFrame:
    """Heart rate rising ``bpm_per_h`` per hour at the same power (durability fade)."""
    return frame.with_columns((pl.col("hr") + bpm_per_h * pl.col("t_s") / 3600.0).alias("hr"))


def _wellness(s: Session) -> None:
    n = (WELLNESS_TO - WELLNESS_FROM).days + 1
    for i in range(n):
        day = WELLNESS_FROM + dt.timedelta(days=i)
        dip = HRV_DIP[0] <= day <= HRV_DIP[1]
        s.add(
            WellnessDaily(
                athlete_id=1,
                date_local=day,
                ctl=40.0,
                atl=45.0,
                hrv=(42.0 if dip else 62.0) + 4.0 * math.sin(i / 2.0),
                resting_hr=(54.0 if dip else 48.0) + (i % 3),
                sleep_s=int((6.0 if dip else 7.2) * 3600 + 600 * math.cos(i / 3.0)),
                raw_json={"sportInfo": [{"type": "Ride", "eftp": 235.0 + 0.15 * i}]},
            )
        )


def _plan(
    s: Session,
    day: dt.date,
    template_id: str,
    params: dict[str, int],
    tss: float,
    minutes: int,
    *,
    indoor: bool,
) -> None:
    t = load_library()[template_id]
    s.add(
        PlannedWorkout(
            athlete_id=1,
            date_local=day,
            slot=1,
            external_id=f"cyp:test:{day.isoformat()}",
            template_id=template_id,
            template_version="1",
            name=resolve(t, dict(params), outdoor=not indoor).name_zh,
            intent=t.intent,
            steps={"params": dict(params), "role": "hit"},
            target_tss=tss,
            target_duration_s=minutes * 60,
            indoor=indoor,
            status="proposed",
        )
    )


def _segs(*parts: Sequence[Seg]) -> list[Seg]:
    return [seg for p in parts for seg in p]


def _reps(n: int, work_s: int, w: float, rest_s: int, rest_w: float = 130.0) -> list[Seg]:
    out: list[Seg] = []
    for i in range(n):
        out.append(Seg(work_s, w))
        if i < n - 1:
            out.append(Seg(rest_s, rest_w))
    return out


def seed_digest_world(factory: sessionmaker[Session], store: StreamStore) -> dict[str, int]:
    """Populate an empty store and run analysis, trends and readiness; returns ride ids."""
    ids: dict[str, int] = {}
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
        _wellness(s)
        idx = 0

        def ride(
            key: str,
            day: dt.date,
            segs: list[Seg],
            *,
            indoor: bool,
            drift: float = 0.0,
            temp_c: float = 22.0,
            rpe: int | None = None,
            feel: int | None = None,
        ) -> None:
            nonlocal idx
            idx += 1
            frame = build_stream(segs, temp_c=None if indoor else temp_c)
            if drift:
                frame = _drift(frame, drift)
            a = _activity(
                s, store, day, idx, frame, indoor=indoor, name=key, icu_rpe=rpe, feel=feel
            )
            ids[key] = a.id

        # ---- September: climb history (22 °C) and the durability baseline
        for k, (day, w) in enumerate(
            zip(
                (
                    dt.date(2026, 9, 9),
                    dt.date(2026, 9, 16),
                    dt.date(2026, 9, 23),
                    dt.date(2026, 9, 30),
                ),
                (210.0, 230.0, 220.0, 240.0),
                strict=True,
            )
        ):
            segs = [*warmup_and_climb(w, 3.6 + 0.1 * k), Seg(240, 0.0, 10.0, -CLIMB_GRADE)]
            ride(f"sep_climb_{k}", day, [*segs, Seg(2400, 170.0)], indoor=False)
        ride("sep_long", dt.date(2026, 9, 26), [Seg(9000, 165.0)], indoor=False, drift=3.0)

        # ---- week A (indoor): achieved, missed, overdone, skipped, achieved, unplanned
        _plan(
            s,
            dt.date(2026, 10, 6),
            "ss_3x_n",
            {"reps": 3, "work_min": 10, "pct": 88},
            70.0,
            60,
            indoor=True,
        )
        ride(
            "a_ss",
            dt.date(2026, 10, 6),
            _segs([Seg(600, 150.0)], _reps(3, 600, 222.0, 240), [Seg(600, 140.0)]),
            indoor=True,
            rpe=6,
            feel=2,
        )
        _plan(s, dt.date(2026, 10, 7), "vo2_5x3", {"reps": 5, "pct": 115}, 75.0, 60, indoor=True)
        ride(
            "a_vo2",
            dt.date(2026, 10, 7),
            _segs([Seg(900, 150.0)], _reps(5, 180, 240.0, 180), [Seg(600, 140.0)]),
            indoor=True,
            rpe=9,
            feel=4,
        )
        _plan(s, dt.date(2026, 10, 8), "recovery_spin", {"total_min": 45}, 20.0, 45, indoor=True)
        ride("a_recovery", dt.date(2026, 10, 8), [Seg(5400, 175.0)], indoor=True)
        _plan(s, dt.date(2026, 10, 9), "z2_endurance_60", {}, 40.0, 60, indoor=True)
        _plan(s, dt.date(2026, 10, 10), "z2_endurance_120", {}, 85.0, 120, indoor=True)
        ride("a_long", dt.date(2026, 10, 10), [Seg(7800, 165.0)], indoor=True, drift=6.0)
        ride(
            "a_free",
            dt.date(2026, 10, 11),
            [
                *warmup_and_climb(262.0, 4.2),  # the climb's best time, then 20 min at 262 W
                Seg(676, 262.0),
                Seg(240, 0.0, 10.0, -CLIMB_GRADE),
                Seg(300, 140.0),
                Seg(300, 300.0),  # 5 and 10 min efforts: the CP fit's short points
                Seg(300, 140.0),
                Seg(600, 285.0),
                Seg(1200, 160.0),
            ],
            indoor=False,
            temp_c=20.0,
        )

        # ---- week B (outdoor): stops and heat, a town ride, a long ride with the climb
        _plan(
            s,
            dt.date(2026, 10, 13),
            "ss_3x_n",
            {"reps": 3, "work_min": 10, "pct": 88},
            75.0,
            70,
            indoor=False,
        )
        ride("b_ss_heat", dt.date(2026, 10, 13), report_ride_segments(), indoor=False, temp_c=31.0)
        _plan(s, dt.date(2026, 10, 15), "z2_endurance_90", {}, 60.0, 90, indoor=False)
        # Ten stops; the last one ends the ride, so the context model counts nine.
        town = [seg for _ in range(10) for seg in (Seg(480, 160.0), Seg(45, 0.0, stop=True))]
        ride("b_town", dt.date(2026, 10, 15), town, indoor=False, temp_c=24.0)
        _plan(s, dt.date(2026, 10, 17), "z2_endurance_120", {}, 85.0, 120, indoor=False)
        ride(
            "b_long",
            dt.date(2026, 10, 17),
            [*warmup_and_climb(245.0, 4.0), Seg(240, 0.0, 10.0, -CLIMB_GRADE), Seg(6000, 165.0)],
            indoor=False,
            drift=4.0,
            temp_c=23.0,
        )

        # ---- week C (trip): planned before the trip was entered, nothing ridden
        _plan(
            s,
            dt.date(2026, 10, 20),
            "ss_3x_n",
            {"reps": 3, "work_min": 10, "pct": 88},
            70.0,
            60,
            indoor=True,
        )
        _plan(s, dt.date(2026, 10, 24), "z2_endurance_120", {}, 85.0, 120, indoor=False)

        # ---- week D (recovery week) and the first day of November
        _plan(s, dt.date(2026, 10, 27), "recovery_spin", {"total_min": 45}, 20.0, 45, indoor=True)
        ride("d_recovery", dt.date(2026, 10, 27), [Seg(2700, 125.0)], indoor=True)
        _plan(s, dt.date(2026, 10, 29), "z2_endurance_60", {}, 40.0, 60, indoor=True)
        ride("d_endurance", dt.date(2026, 10, 29), [Seg(3600, 160.0)], indoor=True)
        ride("nov_free", dt.date(2026, 11, 1), [Seg(3600, 170.0)], indoor=True)

        # ---- stored plan for the next month
        _plan(
            s,
            dt.date(2026, 11, 3),
            "ss_3x_n",
            {"reps": 3, "work_min": 10, "pct": 88},
            70.0,
            60,
            indoor=True,
        )
        _plan(s, dt.date(2026, 11, 5), "z2_endurance_90", {}, 60.0, 90, indoor=True)
        s.commit()
    analyze_pending(factory, store=store)
    build_trends(factory, store, as_of=WELLNESS_TO)
    first = dt.date(2026, 9, 1)
    run_readiness(
        factory, [first + dt.timedelta(days=i) for i in range((WELLNESS_TO - first).days + 1)]
    )
    return ids
