"""Unified activity record, stream vocabulary and intervals.

Mirrors docs/03 ``activities`` / ``stream_files`` / ``activity_intervals``.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["strava", "intervals", "cyp"]
MatchMethod = Literal["strava_id", "time_window", "single_source"]

RIDE_SPORT_TYPES: frozenset[str] = frozenset(
    {
        "Ride",
        "VirtualRide",
        "GravelRide",
        "MountainBikeRide",
        "EBikeRide",
        "EMountainBikeRide",
        "Handcycle",
        "Velomobile",
    }
)


class StreamName(StrEnum):
    """Column names of the fixed Parquet stream schema (docs/03 ``stream_files``)."""

    T_S = "t_s"
    DIST_M = "dist_m"
    LAT = "lat"
    LNG = "lng"
    ALT_M = "alt_m"
    SPEED_MPS = "speed_mps"
    HR = "hr"
    CAD = "cad"
    WATTS = "watts"
    WATTS_EST = "watts_est"
    TEMP_C = "temp_c"
    MOVING = "moving"
    GRADE_PCT = "grade_pct"


STREAM_UNITS: dict[StreamName, str] = {
    StreamName.T_S: "s",
    StreamName.DIST_M: "m",
    StreamName.LAT: "deg",
    StreamName.LNG: "deg",
    StreamName.ALT_M: "m",
    StreamName.SPEED_MPS: "m/s",
    StreamName.HR: "bpm",
    StreamName.CAD: "rpm",
    StreamName.WATTS: "W",
    StreamName.WATTS_EST: "W",
    StreamName.TEMP_C: "degC",
    StreamName.MOVING: "bool",
    StreamName.GRADE_PCT: "%",
}


class StageFlags(BaseModel):
    """Which ingest/analysis stages have completed for an activity."""

    model_config = ConfigDict(extra="forbid")

    detail: bool = False
    streams: bool = False
    efforts: bool = False
    zones: bool = False
    intervals: bool = False
    analyzed: bool = False


class Activity(BaseModel):
    """Source-agnostic ride record. Field names mirror the ``activities`` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    strava_id: int | None = None
    intervals_id: str | None = None
    match_method: MatchMethod = "single_source"
    sport_type: str
    name: str | None = None
    description: str | None = None
    start_utc: dt.datetime
    start_local: dt.datetime | None = None
    tz: str = "Asia/Taipei"
    moving_s: int = Field(ge=0)
    elapsed_s: int = Field(ge=0)
    distance_m: float | None = Field(default=None, ge=0)
    elev_gain_m: float | None = Field(default=None, ge=0)
    trainer: bool = False
    commute: bool = False
    race: bool = False
    manual: bool = False
    has_power: bool = False
    has_hr: bool = False
    has_cadence: bool = False
    device_name: str | None = None
    gear_id: str | None = None
    gear_name: str | None = None
    avg_w: float | None = None
    np_w: float | None = None
    max_w: float | None = None
    kj: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cad: float | None = None
    icu_training_load: float | None = None
    icu_intensity: float | None = None
    icu_ftp: float | None = None
    icu_eftp: float | None = None
    icu_pm_cp: float | None = None
    icu_pm_w_prime: float | None = None
    icu_pm_p_max: float | None = None
    icu_decoupling: float | None = None
    icu_polarization_index: float | None = None
    icu_joules_above_ftp: float | None = None
    icu_max_wbal_depletion: float | None = None
    icu_zone_times: list[float] | None = None
    icu_hr_zone_times: list[float] | None = None
    paired_event_id: int | None = None
    icu_rpe: int | None = Field(default=None, ge=0, le=10)
    feel: int | None = Field(default=None, ge=0, le=5)
    stage_flags: StageFlags = Field(default_factory=StageFlags)
    raw_strava_json: dict[str, Any] | None = None
    raw_intervals_json: dict[str, Any] | None = None

    @property
    def is_ride(self) -> bool:
        """True for cycling sport types; other sports are kept for load only."""
        return self.sport_type in RIDE_SPORT_TYPES


class Interval(BaseModel):
    """A work/recovery segment inside an activity (``activity_intervals``)."""

    model_config = ConfigDict(extra="forbid")

    activity_id: int | None = None
    source: Literal["icu", "cyp"] = "icu"
    idx: int = Field(ge=0)
    label: str | None = None
    type: Literal["WORK", "RECOVERY", "WARMUP", "COOLDOWN", "OTHER"] = "WORK"
    start_s: int = Field(ge=0)
    duration_s: int = Field(ge=0)
    avg_w: float | None = None
    np_w: float | None = None
    intensity: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_cad: float | None = None
    decoupling: float | None = None
    wbal_start: float | None = None
    wbal_end: float | None = None
    zone: int | None = None
    training_load: float | None = None
