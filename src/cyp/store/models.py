"""SQLAlchemy 2.0 declarative models, one class per table in docs/03-data-model.md.

Conventions: timestamps are ISO-8601 text (``*_utc`` UTC, ``*_local`` athlete-local, ``*_at``
UTC); calendar days are ``Date``; raw payloads and structured blobs are ``JSON`` (portable to
Postgres). No SQLite-specific SQL lives here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonDict = dict[str, Any]
JsonAny = Any


class Base(DeclarativeBase):
    """Declarative base; ``dict``/``list`` annotations map to JSON."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        JsonDict: JSON,
        list[Any]: JSON,
        JsonAny: JSON,
    }


# ------------------------------------------------------------------------------------ athlete


class Athlete(Base):
    __tablename__ = "athletes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strava_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    intervals_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    sex: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    weight_kg: Mapped[float | None] = mapped_column(Float)
    raw_strava_json: Mapped[JsonDict | None] = mapped_column(JSON)
    raw_intervals_json: Mapped[JsonDict | None] = mapped_column(JSON)
    updated_at: Mapped[str | None] = mapped_column(String(32))


class AthleteSettingsHistory(Base):
    __tablename__ = "athlete_settings_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    effective_from: Mapped[dt.date] = mapped_column(Date, index=True)
    ftp: Mapped[float | None] = mapped_column(Float)
    indoor_ftp: Mapped[float | None] = mapped_column(Float)
    eftp: Mapped[float | None] = mapped_column(Float)
    w_prime: Mapped[float | None] = mapped_column(Float)
    p_max: Mapped[float | None] = mapped_column(Float)
    lthr: Mapped[int | None] = mapped_column(Integer)
    max_hr: Mapped[int | None] = mapped_column(Integer)
    resting_hr: Mapped[int | None] = mapped_column(Integer)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    power_zones: Mapped[JsonAny | None] = mapped_column(JSON)
    hr_zones: Mapped[JsonAny | None] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(32))  # icu_sport_settings | icu_eftp | manual
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)


# --------------------------------------------------------------------------------- activities


class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (
        Index("ix_activities_start_utc", "start_utc"),
        Index("ix_activities_is_ride_start", "is_ride", "start_utc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int | None] = mapped_column(ForeignKey("athletes.id"), index=True)
    strava_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    intervals_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    match_method: Mapped[str] = mapped_column(String(16), default="single_source")
    sport_type: Mapped[str] = mapped_column(String(32))
    is_ride: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    start_utc: Mapped[str] = mapped_column(String(32))
    start_local: Mapped[str | None] = mapped_column(String(40))
    tz: Mapped[str | None] = mapped_column(String(64))
    moving_s: Mapped[int | None] = mapped_column(Integer)
    elapsed_s: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    elev_gain_m: Mapped[float | None] = mapped_column(Float)
    trainer: Mapped[bool] = mapped_column(Boolean, default=False)
    commute: Mapped[bool] = mapped_column(Boolean, default=False)
    race: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    has_power: Mapped[bool] = mapped_column(Boolean, default=False)
    has_hr: Mapped[bool] = mapped_column(Boolean, default=False)
    has_cadence: Mapped[bool] = mapped_column(Boolean, default=False)
    device_name: Mapped[str | None] = mapped_column(String(100))
    gear_id: Mapped[str | None] = mapped_column(String(32))
    gear_name: Mapped[str | None] = mapped_column(String(100))
    avg_w: Mapped[float | None] = mapped_column(Float)
    np_w: Mapped[float | None] = mapped_column(Float)
    max_w: Mapped[float | None] = mapped_column(Float)
    kj: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[float | None] = mapped_column(Float)
    max_hr: Mapped[float | None] = mapped_column(Float)
    avg_cad: Mapped[float | None] = mapped_column(Float)
    icu_training_load: Mapped[float | None] = mapped_column(Float)
    icu_intensity: Mapped[float | None] = mapped_column(Float)
    icu_ftp: Mapped[float | None] = mapped_column(Float)
    icu_eftp: Mapped[float | None] = mapped_column(Float)
    icu_pm_cp: Mapped[float | None] = mapped_column(Float)
    icu_pm_w_prime: Mapped[float | None] = mapped_column(Float)
    icu_pm_p_max: Mapped[float | None] = mapped_column(Float)
    icu_decoupling: Mapped[float | None] = mapped_column(Float)
    icu_polarization_index: Mapped[float | None] = mapped_column(Float)
    icu_joules_above_ftp: Mapped[float | None] = mapped_column(Float)
    icu_max_wbal_depletion: Mapped[float | None] = mapped_column(Float)
    icu_zone_times: Mapped[JsonAny | None] = mapped_column(JSON)
    icu_hr_zone_times: Mapped[JsonAny | None] = mapped_column(JSON)
    paired_event_id: Mapped[int | None] = mapped_column(Integer)
    icu_rpe: Mapped[int | None] = mapped_column(Integer)
    feel: Mapped[int | None] = mapped_column(Integer)
    raw_strava_json: Mapped[JsonDict | None] = mapped_column(JSON)
    raw_intervals_json: Mapped[JsonDict | None] = mapped_column(JSON)
    stage_flags: Mapped[JsonDict | None] = mapped_column(JSON)
    # Denormalised "pending" booleans so the sync/analyze queues are indexable.
    pending_detail: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    pending_streams: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    pending_analysis: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    fetched_at: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[str | None] = mapped_column(String(32))

    metrics: Mapped[ActivityMetrics | None] = relationship(back_populates="activity", uselist=False)
    stream_file: Mapped[StreamFile | None] = relationship(back_populates="activity", uselist=False)


class StreamFile(Base):
    __tablename__ = "stream_files"

    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))  # strava | intervals
    path: Mapped[str] = mapped_column(String(500))
    columns: Mapped[JsonAny | None] = mapped_column(JSON)
    hz: Mapped[float] = mapped_column(Float, default=1.0)
    original_size: Mapped[int | None] = mapped_column(Integer)
    resolution: Mapped[str | None] = mapped_column(String(16))
    n_samples: Mapped[int | None] = mapped_column(Integer)
    raw_json_path: Mapped[str | None] = mapped_column(String(500))

    activity: Mapped[Activity] = relationship(back_populates="stream_file")


class ActivityMetrics(Base):
    __tablename__ = "activity_metrics"

    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), primary_key=True)
    algo_version: Mapped[str] = mapped_column(String(32))
    np_w: Mapped[float | None] = mapped_column(Float)
    if_: Mapped[float | None] = mapped_column("if_", Float)
    tss: Mapped[float | None] = mapped_column(Float)
    vi: Mapped[float | None] = mapped_column(Float)
    tss_source: Mapped[str | None] = mapped_column(String(16))  # power | hr | estimated
    ef: Mapped[float | None] = mapped_column(Float)
    decoupling_pct: Mapped[float | None] = mapped_column(Float)
    hr_lag_s: Mapped[float | None] = mapped_column(Float)
    hr_drift_detail: Mapped[JsonAny | None] = mapped_column(JSON)
    power_curve: Mapped[JsonAny | None] = mapped_column(JSON)
    time_in_zone_power: Mapped[JsonAny | None] = mapped_column(JSON)
    time_in_zone_hr: Mapped[JsonAny | None] = mapped_column(JSON)
    climbs: Mapped[JsonAny | None] = mapped_column(JSON)
    efforts: Mapped[JsonAny | None] = mapped_column(JSON)
    pacing: Mapped[JsonAny | None] = mapped_column(JSON)
    wbal_min_j: Mapped[float | None] = mapped_column(Float)
    estimated_power_meta: Mapped[JsonAny | None] = mapped_column(JSON)
    comparison: Mapped[JsonAny | None] = mapped_column(JSON)
    status: Mapped[str | None] = mapped_column(String(16))
    next_recommendation: Mapped[str | None] = mapped_column(String(16))
    explanation: Mapped[JsonDict | None] = mapped_column(JSON)
    computed_at: Mapped[str | None] = mapped_column(String(32))

    activity: Mapped[Activity] = relationship(back_populates="metrics")


class ActivityInterval(Base):
    __tablename__ = "activity_intervals"
    __table_args__ = (UniqueConstraint("activity_id", "source", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    source: Mapped[str] = mapped_column(String(8))  # icu | cyp
    idx: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(100))
    type: Mapped[str | None] = mapped_column(String(16))
    start_s: Mapped[int | None] = mapped_column(Integer)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    avg_w: Mapped[float | None] = mapped_column(Float)
    np_w: Mapped[float | None] = mapped_column(Float)
    intensity: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[float | None] = mapped_column(Float)
    max_hr: Mapped[float | None] = mapped_column(Float)
    avg_cad: Mapped[float | None] = mapped_column(Float)
    decoupling: Mapped[float | None] = mapped_column(Float)
    wbal_start: Mapped[float | None] = mapped_column(Float)
    wbal_end: Mapped[float | None] = mapped_column(Float)
    zone: Mapped[int | None] = mapped_column(Integer)
    training_load: Mapped[float | None] = mapped_column(Float)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)


class Segment(Base):
    """Strava segment (port of strava-analyis ``segments``)."""

    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # strava segment id
    name: Mapped[str | None] = mapped_column(String(300))
    activity_type: Mapped[str | None] = mapped_column(String(32))
    distance_m: Mapped[float | None] = mapped_column(Float)
    average_grade: Mapped[float | None] = mapped_column(Float)
    maximum_grade: Mapped[float | None] = mapped_column(Float)
    elevation_high: Mapped[float | None] = mapped_column(Float)
    elevation_low: Mapped[float | None] = mapped_column(Float)
    climb_category: Mapped[int | None] = mapped_column(Integer)
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    starred: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)
    fetched_at: Mapped[str | None] = mapped_column(String(32))


class SegmentEffort(Base):
    """Strava segment effort (port of strava-analyis ``segment_efforts``)."""

    __tablename__ = "segment_efforts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # strava effort id
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("segments.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(300))
    start_utc: Mapped[str | None] = mapped_column(String(32))
    start_index: Mapped[int | None] = mapped_column(Integer)
    end_index: Mapped[int | None] = mapped_column(Integer)
    elapsed_s: Mapped[int | None] = mapped_column(Integer)
    moving_s: Mapped[int | None] = mapped_column(Integer)
    distance_m: Mapped[float | None] = mapped_column(Float)
    avg_w: Mapped[float | None] = mapped_column(Float)
    device_watts: Mapped[bool | None] = mapped_column(Boolean)
    avg_hr: Mapped[float | None] = mapped_column(Float)
    max_hr: Mapped[float | None] = mapped_column(Float)
    avg_cad: Mapped[float | None] = mapped_column(Float)
    pr_rank: Mapped[int | None] = mapped_column(Integer)
    kom_rank: Mapped[int | None] = mapped_column(Integer)
    achievements: Mapped[JsonAny | None] = mapped_column(JSON)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)
    fetched_at: Mapped[str | None] = mapped_column(String(32))


class ActivityZones(Base):
    """Strava per-activity zone distribution (port of strava-analyis ``activity_zones``)."""

    __tablename__ = "activity_zones"
    __table_args__ = (UniqueConstraint("activity_id", "zone_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), index=True)
    zone_type: Mapped[str] = mapped_column(String(16))  # power | heartrate
    sensor_based: Mapped[bool | None] = mapped_column(Boolean)
    custom_zones: Mapped[bool | None] = mapped_column(Boolean)
    points: Mapped[int | None] = mapped_column(Integer)
    distribution_buckets: Mapped[JsonAny | None] = mapped_column(JSON)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)
    fetched_at: Mapped[str | None] = mapped_column(String(32))


# ---------------------------------------------------------------------------- daily series


class WellnessDaily(Base):
    __tablename__ = "wellness_daily"

    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), primary_key=True)
    date_local: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ctl: Mapped[float | None] = mapped_column(Float)
    atl: Mapped[float | None] = mapped_column(Float)
    ramp_rate: Mapped[float | None] = mapped_column(Float)
    ctl_load: Mapped[float | None] = mapped_column(Float)
    atl_load: Mapped[float | None] = mapped_column(Float)
    resting_hr: Mapped[float | None] = mapped_column(Float)
    hrv: Mapped[float | None] = mapped_column(Float)
    hrv_sdnn: Mapped[float | None] = mapped_column(Float)
    sleep_s: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[float | None] = mapped_column(Float)
    sleep_quality: Mapped[int | None] = mapped_column(Integer)
    avg_sleeping_hr: Mapped[float | None] = mapped_column(Float)
    soreness: Mapped[int | None] = mapped_column(Integer)
    fatigue: Mapped[int | None] = mapped_column(Integer)
    stress: Mapped[int | None] = mapped_column(Integer)
    mood: Mapped[int | None] = mapped_column(Integer)
    motivation: Mapped[int | None] = mapped_column(Integer)
    injury: Mapped[int | None] = mapped_column(Integer)
    readiness_icu: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    vo2max: Mapped[float | None] = mapped_column(Float)
    steps: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[str | None] = mapped_column(Text)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)
    fetched_at: Mapped[str | None] = mapped_column(String(32))


class FitnessDaily(Base):
    __tablename__ = "fitness_daily"

    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), primary_key=True)
    date_local: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    ctl_icu: Mapped[float | None] = mapped_column(Float)
    atl_icu: Mapped[float | None] = mapped_column(Float)
    tsb_icu: Mapped[float | None] = mapped_column(Float)
    ctl_sim: Mapped[float | None] = mapped_column(Float)
    atl_sim: Mapped[float | None] = mapped_column(Float)
    tsb_sim: Mapped[float | None] = mapped_column(Float)
    load_actual: Mapped[float | None] = mapped_column(Float)
    load_planned: Mapped[float | None] = mapped_column(Float)
    acwr_7_28: Mapped[float | None] = mapped_column(Float)
    monotony_7: Mapped[float | None] = mapped_column(Float)
    strain_7: Mapped[float | None] = mapped_column(Float)


class PowerCurveSnapshot(Base):
    __tablename__ = "power_curve_snapshots"
    __table_args__ = (UniqueConstraint("athlete_id", "as_of_date", "window", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, index=True)
    window: Mapped[str] = mapped_column(String(16))  # 42d | 90d | season | all
    durations_s: Mapped[JsonAny | None] = mapped_column(JSON)
    watts: Mapped[JsonAny | None] = mapped_column(JSON)
    w_kg: Mapped[JsonAny | None] = mapped_column(JSON)
    cp: Mapped[float | None] = mapped_column(Float)
    w_prime: Mapped[float | None] = mapped_column(Float)
    p_max: Mapped[float | None] = mapped_column(Float)
    eftp_icu: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(8))  # icu | cyp
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)


class ReadinessDaily(Base):
    __tablename__ = "readiness_daily"

    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), primary_key=True)
    date_local: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    score_0_100: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(16))
    inputs: Mapped[JsonDict | None] = mapped_column(JSON)
    recommendation: Mapped[str | None] = mapped_column(String(16))
    explanation: Mapped[JsonDict | None] = mapped_column(JSON)
    algo_version: Mapped[str | None] = mapped_column(String(32))


# ------------------------------------------------------------------------------------ planning


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    date_local: Mapped[dt.date] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(8))  # RACE_A | RACE_B | RACE_C | TARGET
    kind: Mapped[str] = mapped_column(String(16))
    target: Mapped[JsonDict | None] = mapped_column(JSON)
    icu_event_id: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    goal_id: Mapped[int | None] = mapped_column(ForeignKey("goals.id"))
    status: Mapped[str] = mapped_column(String(16), default="active")
    config: Mapped[JsonDict | None] = mapped_column(JSON)
    created_at: Mapped[str | None] = mapped_column(String(32))

    blocks: Mapped[list[Block]] = relationship(back_populates="season")


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("season_id", "idx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(16))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    weeks: Mapped[int] = mapped_column(Integer)
    load_pattern: Mapped[str] = mapped_column(String(8), default="3:1")
    focus: Mapped[JsonDict | None] = mapped_column(JSON)

    season: Mapped[Season] = relationship(back_populates="blocks")
    week_plans: Mapped[list[WeekPlan]] = relationship(back_populates="block")


class WeekPlan(Base):
    __tablename__ = "week_plans"
    __table_args__ = (UniqueConstraint("block_id", "week_start"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id"), index=True)
    week_start: Mapped[dt.date] = mapped_column(Date, index=True)
    target_tss: Mapped[float | None] = mapped_column(Float)
    target_hours: Mapped[float | None] = mapped_column(Float)
    hit_sessions: Mapped[int | None] = mapped_column(Integer)
    planned_ctl_end: Mapped[float | None] = mapped_column(Float)
    template_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    notes: Mapped[str | None] = mapped_column(Text)

    block: Mapped[Block] = relationship(back_populates="week_plans")
    workouts: Mapped[list[PlannedWorkout]] = relationship(back_populates="week_plan")


class PlannedWorkout(Base):
    __tablename__ = "planned_workouts"
    __table_args__ = (Index("ix_planned_workouts_date_slot", "date_local", "slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_plan_id: Mapped[int | None] = mapped_column(ForeignKey("week_plans.id"), index=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"), index=True)
    date_local: Mapped[dt.date] = mapped_column(Date)
    slot: Mapped[int] = mapped_column(Integer, default=1)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    template_id: Mapped[str | None] = mapped_column(String(64))
    template_version: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    intent: Mapped[str] = mapped_column(String(16))
    steps: Mapped[JsonAny | None] = mapped_column(JSON)
    workout_text: Mapped[str | None] = mapped_column(Text)
    target_tss: Mapped[float | None] = mapped_column(Float)
    target_duration_s: Mapped[int | None] = mapped_column(Integer)
    target_if: Mapped[float | None] = mapped_column(Float)
    indoor: Mapped[bool] = mapped_column(Boolean, default=False)
    target_mode: Mapped[str] = mapped_column(String(8), default="POWER")
    status: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    icu_event_id: Mapped[int | None] = mapped_column(Integer)
    icu_training_load_readback: Mapped[float | None] = mapped_column(Float)
    executed_activity_id: Mapped[int | None] = mapped_column(ForeignKey("activities.id"))
    compliance: Mapped[JsonDict | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(8), default="planner")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    explanation: Mapped[JsonDict | None] = mapped_column(JSON)

    week_plan: Mapped[WeekPlan | None] = relationship(back_populates="workouts")
    publish_log: Mapped[list[PublishLog]] = relationship(back_populates="planned_workout")


class PlanRevision(Base):
    __tablename__ = "plan_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(16))  # daily|weekly|manual|llm|goal_change
    reason: Mapped[str | None] = mapped_column(Text)
    horizon_start: Mapped[dt.date | None] = mapped_column(Date)
    horizon_end: Mapped[dt.date | None] = mapped_column(Date)
    before: Mapped[JsonAny | None] = mapped_column(JSON)
    after: Mapped[JsonAny | None] = mapped_column(JSON)
    diff: Mapped[JsonAny | None] = mapped_column(JSON)
    explanation: Mapped[JsonDict | None] = mapped_column(JSON)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)


class PublishLog(Base):
    __tablename__ = "publish_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("job_runs.id"), index=True)
    planned_workout_id: Mapped[int | None] = mapped_column(
        ForeignKey("planned_workouts.id"), index=True
    )
    action: Mapped[str] = mapped_column(String(8))  # create|update|delete|noop
    external_id: Mapped[str | None] = mapped_column(String(64))
    icu_event_id: Mapped[int | None] = mapped_column(Integer)
    request: Mapped[JsonAny | None] = mapped_column(JSON)
    response: Mapped[JsonAny | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # ok|failed|needs_review
    verified_load: Mapped[float | None] = mapped_column(Float)
    at: Mapped[str] = mapped_column(String(32))

    planned_workout: Mapped[PlannedWorkout | None] = relationship(back_populates="publish_log")


class WorkoutTemplate(Base):
    __tablename__ = "workout_templates"
    __table_args__ = (UniqueConstraint("id", "version"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    intent: Mapped[str] = mapped_column(String(16))
    phase_tags: Mapped[JsonAny | None] = mapped_column(JSON)
    min_ftp_pct: Mapped[float | None] = mapped_column(Float)
    steps_template: Mapped[JsonAny | None] = mapped_column(JSON)
    duration_range_s: Mapped[JsonAny | None] = mapped_column(JSON)
    tss_formula: Mapped[str | None] = mapped_column(String(200))
    indoor_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    outdoor_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    requires_power: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)


class IcuEvent(Base):
    """Raw mirror of intervals.icu calendar events (ours and the athlete's own).

    ``id`` is the icu event id. Our planned workouts live in ``planned_workouts``; this table is
    the read-back of everything on the calendar so the planner can see races, notes and
    availability the athlete entered directly (docs/02 §3.2 ``events`` + ``fitness-model-events``).
    """

    __tablename__ = "icu_events"
    __table_args__ = (Index("ix_icu_events_start_category", "start_date_local", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str | None] = mapped_column(String(16))
    start_date_local: Mapped[str | None] = mapped_column(String(40))
    end_date_local: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str | None] = mapped_column(String(32))
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    icu_training_load: Mapped[float | None] = mapped_column(Float)
    training_availability: Mapped[str | None] = mapped_column(String(16))
    max_training_time: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[JsonDict | None] = mapped_column(JSON)
    fetched_at: Mapped[str | None] = mapped_column(String(32))


# ------------------------------------------------------------------------------------ operations


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_started", "job", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[str] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))  # running|ok|failed
    counts: Mapped[JsonDict | None] = mapped_column(JSON)
    rate_limit_snapshot: Mapped[JsonDict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(String(500))


class SyncCursor(Base):
    __tablename__ = "sync_cursors"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[str | None] = mapped_column(String(32))


class SchemaMeta(Base):
    """Free-form key/value metadata (algo versions, data-dir layout version, …)."""

    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(String(32))


#: Tables that carry a persisted ``Explanation`` JSON column (docs/07 §1), keyed by the
#: column used to look an explanation up by its ``Explanation.key``.
EXPLANATION_TABLES: tuple[type[Base], ...] = (
    ActivityMetrics,
    ReadinessDaily,
    PlannedWorkout,
    PlanRevision,
)
