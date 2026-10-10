"""Pydantic response / request models: the contract between the backend and the frontend.

Every service in :mod:`cyp.services` returns these models; the HTTP API serialises them as-is,
so the OpenAPI document generated from them (``cyp dev openapi``) is the frontend's source of
truth. Conventions: dates are ISO ``YYYY-MM-DD``; durations in seconds unless the field says
``_min``; power in W; loads in TSS units; ``*_zh`` fields are display text in Traditional
Chinese; every verdict carries an ``explanation`` (:class:`~cyp.core.explain.Explanation`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cyp.core.explain import Explanation


class Model(BaseModel):
    """Base: forbid unknown fields on requests, keep responses strict and documented."""

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------ meta


class AthleteSummary(Model):
    """Who the data belongs to and their current anchors."""

    athlete_id: int
    name: str | None = None
    weight_kg: float | None = None
    ftp: float | None = Field(None, description="FTP effective today (W)")
    w_kg: float | None = None
    timezone: str


class SeasonSummary(Model):
    """Where today sits in the season."""

    start: dt.date
    goal_date: dt.date
    goal_name: str | None = None
    target_ftp: float | None = None
    week_index: int | None = Field(None, description="1-based season week of today")
    weeks_total: int
    phase: str | None = None


class Meta(Model):
    """``GET /v1/meta``: everything the app shell needs on load."""

    version: str
    today: dt.date
    data_version: str
    athlete: AthleteSummary | None
    season: SeasonSummary | None = None
    last_jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plan_mode: Literal["propose", "apply"] = "propose"


# --------------------------------------------------------------------------------- fitness


class FitnessPoint(Model):
    """One day of the performance-management chart."""

    date: dt.date
    load: float = Field(description="Actual daily load (all sports)")
    ctl: float | None = None
    atl: float | None = None
    tsb: float | None = None
    source: Literal["icu", "sim", "none"] = "none"
    ctl_sim: float | None = None
    acwr: float | None = None
    monotony: float | None = None
    planned_load: float | None = Field(
        None, description="Planned load: the calendar's WORKOUT events, else our live proposals"
    )


class FitnessSeries(Model):
    """``GET /v1/fitness``."""

    start: dt.date
    end: dt.date
    points: list[FitnessPoint]


# ------------------------------------------------------------------------------ activities


class ActivitySummary(Model):
    """Row of the activity list."""

    id: int
    date: dt.date
    name: str | None
    sport: str
    is_ride: bool
    moving_s: int
    load: float
    load_source: str
    tss: float | None = None
    np_w: float | None = None
    intensity_factor: float | None = None
    ef: float | None = None
    decoupling_pct: float | None = None
    classification: str | None = None
    classification_zh: str | None = None
    status: str | None = None
    climbs: int = 0


class ActivityPage(Model):
    """``GET /v1/activities``."""

    total: int
    offset: int
    limit: int
    items: list[ActivitySummary]


class ActivityDetail(Model):
    """``GET /v1/activities/{id}``: the stored per-ride analysis."""

    summary: ActivitySummary
    distance_m: float | None = None
    elev_gain_m: float | None = None
    avg_w: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    vi: float | None = None
    hr_lag_s: float | None = None
    power_curve: dict[str, float] = Field(default_factory=dict)
    time_in_zone_power: dict[str, float] | None = None
    time_in_zone_hr: dict[str, float] | None = None
    climbs: list[dict[str, Any]] = Field(default_factory=list)
    efforts: list[dict[str, Any]] = Field(default_factory=list)
    pacing: dict[str, Any] = Field(default_factory=dict)
    durability: dict[str, Any] | None = None
    next_recommendation: str | None = None
    explanation: Explanation | None = None
    has_streams: bool = False


class StreamSeries(Model):
    """``GET /v1/activities/{id}/streams``: column arrays, downsampled for charts."""

    activity_id: int
    resolution_s: int
    n: int
    columns: dict[str, list[float | None]]


# ------------------------------------------------------------------------------------- FTP


class CPFitOut(Model):
    """One CP model fit."""

    cp: float
    w_prime: float
    p_max: float | None = None
    r2: float | None = None
    rmse_w: float | None = None
    eftp: float
    points: list[list[float]]
    cp_diff_vs_icu_pct: float | None = None


class PowerWindow(Model):
    """Mean-maximal power + fits for one window."""

    window: str
    mmp: dict[str, float]
    cp_2p: CPFitOut | None = None
    cp_3p: CPFitOut | None = None
    icu: dict[str, float | None] | None = None


class FtpProposalOut(Model):
    """A suggested FTP change — never applied automatically."""

    proposed_ftp: float | None
    direction: Literal["up", "down", "none"]
    days_sustained: int
    median_estimate: float | None
    change_pct: float | None
    best_20min_w: float | None
    unsupported: bool
    insufficient_evidence: bool = False
    sources: list[str]


class EstimatePoint(Model):
    """Daily FTP estimate."""

    date: dt.date
    ftp: float


class FtpStatusOut(Model):
    """``GET /v1/ftp`` / ``POST /v1/ftp/what-if``."""

    as_of: dt.date
    current_ftp: float | None
    w_kg: float | None
    windows: dict[str, PowerWindow]
    estimate_source: str
    max_efforts: dict[str, Any] | None = None
    power_unreliable_excluded: int = 0
    estimates: list[EstimatePoint]
    proposal: FtpProposalOut | None
    history: list[dict[str, Any]]
    explanations: list[Explanation]
    compute_ms: float | None = None


class FtpWhatIf(Model):
    """Request body of ``POST /v1/ftp/what-if``."""

    as_of: dt.date | None = None
    ftp: float | None = Field(None, gt=50, lt=700)


class FtpAccept(Model):
    """Request body of ``POST /v1/ftp/accept`` (the athlete's explicit decision)."""

    ftp: float = Field(gt=50, lt=700)
    effective_from: dt.date | None = None
    note: str | None = None


# ------------------------------------------------------------------------------- readiness


class ReadinessComponentOut(Model):
    """One weighted input of the readiness score."""

    name: str
    name_zh: str
    z: float
    share: float = Field(description="Renormalised weight in the score (0–1)")


class ReadinessOut(Model):
    """``GET /v1/readiness/{date}``."""

    date: dt.date
    score: float
    status: str
    recommendation: str
    recommendation_zh: str
    components: list[ReadinessComponentOut]
    missing: list[str]
    rule_hits: list[str]
    explanation: Explanation | None = None


# ------------------------------------------------------------------------------------ plan


class PlanStep(Model):
    """A rendered workout step (for the workout-profile chart)."""

    kind: str
    duration_s: int
    lo: float | None = None
    hi: float | None = None
    cadence: str | None = None
    cue: str | None = None
    repeat: int = 1


class PlannedDayOut(Model):
    """One day of the horizon."""

    date: dt.date
    role: str
    role_zh: str
    template_id: str | None = None
    name_zh: str | None = None
    intent: str
    tss: float
    minutes: int
    max_minutes: int
    outdoor: bool
    params: dict[str, int] = Field(default_factory=dict)
    steps: list[PlanStep] = Field(default_factory=list)
    workout_text: str | None = None
    note_zh: str | None = None
    mutable: bool = True
    explanation: Explanation | None = None


class WeekTargetOut(Model):
    """Load target of one season week."""

    week_start: dt.date
    index: int
    phase: str
    phase_zh: str
    recovery: bool
    test: str | None = None
    target_tss: float
    target_hours: float
    ctl_start: float
    ctl_end: float
    planned_tss: float | None = None
    planned_hours: float | None = None
    explanation: Explanation | None = None


class PlanOut(Model):
    """``GET /v1/plan`` and ``POST /v1/plan/preview``."""

    today: dt.date
    season_key: str
    persisted: bool
    days: list[PlannedDayOut]
    weeks: list[WeekTargetOut]
    adaptations: list[Explanation]
    repairs: list[Explanation]
    violations: list[dict[str, Any]]
    needs_review: bool
    projection: list[FitnessPoint] = Field(
        default_factory=list, description="Simulated CTL/ATL/TSB over the horizon"
    )
    changes: dict[str, list[str]] = Field(default_factory=dict)
    compute_ms: float | None = None


class PlanPreviewRequest(Model):
    """What-if inputs for ``POST /v1/plan/preview`` (nothing is stored)."""

    today: dt.date | None = None
    weekday_minutes: dict[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"], int] = Field(
        default_factory=dict
    )
    weekly_max_minutes: int | None = Field(None, ge=0, le=2400)
    date_minutes: dict[dt.date, int] = Field(
        default_factory=dict, description="Cap minutes on dates; 0 = day off"
    )
    indoor_days: list[dt.date] = Field(default_factory=list)
    readiness: Literal["REST", "EASY", "AS_PLANNED", "UPGRADE"] | None = None
    bias: dict[str, float] | None = None
    horizon_days: int | None = Field(None, ge=1, le=56)
    ctl: float | None = Field(None, ge=0, le=200)
    atl: float | None = Field(None, ge=0, le=300)


class SeasonWeekOut(Model):
    """One week of the season overview."""

    index: int
    start: dt.date
    phase: str
    phase_zh: str
    recovery: bool
    test: str | None
    hit_sessions: int
    target_tss: float
    target_hours: float
    ctl_end: float
    checkpoint_ftp: float | None = None
    #: Long-ride minutes from a distance goal's progression (``None`` = the configured value).
    long_ride_minutes: int | None = None
    over_distance: bool = False


class SeasonOut(Model):
    """``GET /v1/season``: the whole macro plan as projected from today's CTL."""

    start: dt.date
    goal_date: dt.date
    goal_name: str | None
    target_ftp: float | None
    ctl_start: float
    weeks: list[SeasonWeekOut]


# -------------------------------------------------------------------------------- calendar

DayStatus = Literal["done", "over", "under", "skipped", "extra", "pending", "planned", "rest"]


class CalendarPlanned(Model):
    """The workout planned for a day: what the intervals.icu calendar shows, else our proposal."""

    name_zh: str
    source: Literal["calendar", "proposal"]
    tss: float | None = None
    minutes: int | None = None
    role: str | None = None  # known when the workout is one of ours
    outdoor: bool | None = None


class CalendarActivity(Model):
    """One activity of a day, reduced to what a calendar cell needs."""

    id: int
    name: str | None
    sport: str
    is_ride: bool
    minutes: int
    load: float
    classification_zh: str | None = None
    status: str | None = None


class CalendarDay(Model):
    """One cell of the calendar."""

    date: dt.date
    status: DayStatus
    planned: CalendarPlanned | None = None
    activities: list[CalendarActivity]
    load: float  # Σ load of the day's rides
    notes: list[str]  # the athlete's own events (sick, race, …)
    readiness_score: float | None = None
    readiness_zh: str | None = None
    ctl: float | None = None
    tsb: float | None = None


class CalendarWeek(Model):
    """Summary of one Monday–Sunday week."""

    start: dt.date
    load_done: float
    load_planned: float  # Σ planned over the week (past days as the calendar showed them)
    target_tss: float | None = None  # season target of the week
    hours_done: float
    ctl_end: float | None = None
    phase_zh: str | None = None
    recovery: bool = False


class CalendarOut(Model):
    """``GET /v1/calendar``: whole weeks covering ``[start, end]``."""

    today: dt.date
    start: dt.date
    end: dt.date
    days: list[CalendarDay]
    weeks: list[CalendarWeek]


# ----------------------------------------------------------------------------- trends/misc


class ExplainOut(Model):
    """``GET /v1/explain/{key}``."""

    key: str
    source: str
    explanation: dict[str, Any]


class ReportOut(Model):
    """``GET /v1/reports/{kind}/{stem}``."""

    kind: Literal["daily", "weekly"]
    stem: str
    markdown: str
    facts: dict[str, Any]


class JobOut(Model):
    """A background job (sync / analyze / recompute)."""

    id: str
    kind: str
    status: Literal["queued", "running", "ok", "failed"]
    #: Profile the job runs for ("" = the server's default profile).
    profile: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


# ------------------------------------------------------------------- profiles / autopilot (web UI)


class ProfileOut(Model):
    """``GET /v1/profiles``: one athlete profile (no secrets)."""

    slug: str
    display_name: str
    default: bool
    icu_athlete_id: str | None = None
    planner_mode: str | None = None
    key_set: bool
    last_run: str | None = None
    last_run_status: str | None = None
    garmin_upload_workouts: bool | None = None
    calendar_write_allowed: bool = False
    strava_write_allowed: bool = False


class AutopilotRequest(Model):
    """``POST /v1/autopilot``: run the morning now. ``write`` needs ``confirm``."""

    write: bool = False
    confirm: bool = False


class AutopilotStage(Model):
    """One stage of an autopilot run."""

    name: str
    status: str
    detail: str = ""


class AutopilotRunOut(Model):
    """``GET /v1/autopilot/runs``: a saved run report."""

    profile: str
    day: dt.date
    planner_mode: str
    ok: bool
    at: str | None = None
    stages: list[AutopilotStage] = Field(default_factory=list)


class RideLogOut(Model):
    """``GET /v1/activities/{id}/ride-log``: the saved text (with poem) or the generated one."""

    activity_id: int
    workout: str
    ride_log: str
    text: str
    #: ``saved`` = an edited text was stored for this ride; ``generated`` = numbers only.
    source: str = "generated"
    generated: str = ""


#: Upper bound for a RIDE.LOG text (a long poem fits many times over).
RIDE_LOG_MAX_CHARS = 10_000


class RideLogSave(Model):
    """``POST /v1/activities/{id}/ride-log``: store an edited text ("" = back to generated)."""

    text: str = Field(max_length=RIDE_LOG_MAX_CHARS)


class StravaDescriptionRequest(Model):
    """``POST /v1/activities/{id}/strava-description``: preview, or write with ``confirm``."""

    text: str = Field(max_length=RIDE_LOG_MAX_CHARS)
    confirm: bool = False


class StravaDescriptionOut(Model):
    """Result of a preview or a write."""

    strava_id: int
    before: str
    after: str
    written: bool


class RideFeedbackIn(Model):
    """``POST /v1/activities/{id}/feedback``: post-ride RPE 1–10 and feel 1 (strong) … 5 (weak)."""

    rpe: int | None = None
    feel: int | None = None


class RideFeedbackOut(Model):
    """Answers for a ride and where they came from (``web`` / ``intervals.icu`` / ``none``)."""

    activity_id: int
    rpe: int | None = None
    feel: int | None = None
    source: str
