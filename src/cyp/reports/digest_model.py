"""Data contract of the weekly and monthly digests (docs/specs/digest-reports.md).

``reports.digest_facts`` fills a :class:`WeeklyDigest` / :class:`MonthlyDigest`;
``reports.digest_pdf`` renders it; nothing in between computes (docs/07). Section numbers match
the spec tables. Optional sections are ``None`` when their inputs are missing; the renderer
then leaves the section out. ``is_break`` marks a period without rides (short version).
Shared building blocks come from the per-ride contract.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cyp.reports.ride_model import ClimbCompare, FtpPoint, Grade, MethodUsed

WEEKLY_VERSION = "digest_weekly_v1"
MONTHLY_VERSION = "digest_monthly_v1"

#: Per planned session: the ride's intent verdict, ``overdone`` for an easy day ridden far past
#: its load, ``skipped`` when nothing was ridden, ``unplanned`` for a ride without a plan.
SessionVerdict = Literal["achieved", "partial", "missed", "overdone", "skipped", "unplanned"]
Band = Literal["low", "mid", "high"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DigestHeadline(_Model):
    """Section 1: the period's verdict and the one thing to change."""

    text_zh: str
    verdict: Literal["on_track", "partly", "off_track", "break", "no_data"]
    change_zh: str = ""


class Period(_Model):
    """The period covered and why it may be a break."""

    start: dt.date
    end: dt.date
    label_zh: str
    season_week: int | None = None
    phase: str | None = None
    is_break: bool = False
    break_reason_zh: str = ""


class SessionLine(_Model):
    """One planned session and/or ride on one day (section 2 grid)."""

    day: dt.date
    planned_name: str | None = None
    planned_tss: float | None = None
    ride_name: str | None = None
    activity_id: int | None = None
    actual_tss: float | None = None
    moving_min: int | None = None
    outdoor: bool | None = None
    verdict: SessionVerdict
    note_zh: str = ""


class Load(_Model):
    """Section 3: load planned vs actual and the fitness curve."""

    planned_tss: float
    actual_tss: float
    hours: float
    rides: int
    ctl_start: float | None = None
    ctl_end: float | None = None
    atl_end: float | None = None
    tsb_end: float | None = None
    ramp_per_week: float | None = None
    ramp_cap: float | None = None
    daily: list[tuple[dt.date, float, float]] = Field(
        default_factory=list, description="(day, planned TSS, actual TSS)"
    )
    ctl_series: list[tuple[dt.date, float]] = Field(default_factory=list)
    explanation: dict[str, Any] | None = None


class Distribution(_Model):
    """Section 4 (weekly) / 6 (monthly): intensity by sessions vs the phase target."""

    sessions: dict[Band, int]
    session_frac: dict[Band, float]
    target_frac: dict[Band, float] | None = None
    time_frac: dict[Band, float] | None = None
    verdict: Literal["under", "on", "over", "no_target"]
    explanation: dict[str, Any] | None = None


class GoalProgress(_Model):
    """Section 5 (weekly) / part of 5 (monthly): FTP target, now vs the period start."""

    target_w: float
    target_date: dt.date
    current_ftp_w: float
    trend_level_w: float | None = None
    projected_date: dt.date | None = None
    projected_date_before: dt.date | None = None
    evidence_w: float | None = None
    history: list[FtpPoint] = Field(default_factory=list)
    text_zh: str = ""
    explanation: dict[str, Any] | None = None


class RecoveryDay(_Model):
    """One day of recovery signals."""

    day: dt.date
    hrv_z7: float | None = None
    rhr: float | None = None
    sleep_h: float | None = None
    readiness: str | None = None
    feel_gap: float | None = None


class Recovery(_Model):
    """Section 6 (weekly) / 8 (monthly): HRV band, RHR, sleep, feel vs expected, readiness."""

    days: list[RecoveryDay] = Field(default_factory=list)
    rest_days: int = 0
    readiness_counts: dict[str, int] = Field(default_factory=dict)
    text_zh: str = ""
    explanation: dict[str, Any] | None = None


class Highlights(_Model):
    """Section 7 (weekly): climbs vs history and durability vs the 4-week median."""

    climbs: list[ClimbCompare] = Field(default_factory=list)
    durability_w: float | None = None
    durability_median_w: float | None = None
    text_zh: str = ""


class PlannedSession(_Model):
    """One stored session ahead."""

    day: dt.date
    name: str
    purpose_zh: str = ""
    tss: float | None = None
    minutes: int | None = None
    outdoor: bool | None = None


class NextPlan(_Model):
    """Section 8 (weekly) / 9 (monthly): what the stored plan holds next."""

    sessions: list[PlannedSession] = Field(default_factory=list)
    phase_zh: str = ""
    key_sessions_zh: list[str] = Field(default_factory=list)
    text_zh: str = ""


class WeeklyDigest(_Model):
    """Everything the weekly PDF shows."""

    version: str = WEEKLY_VERSION
    profile: str
    athlete_name: str
    #: Timezone-aware; renderers convert it to the athlete's local time.
    generated_at: dt.datetime
    period: Period
    headline: DigestHeadline
    sessions: list[SessionLine] = Field(default_factory=list)
    load: Load | None = None
    distribution: Distribution | None = None
    goal: GoalProgress | None = None
    recovery: Recovery | None = None
    highlights: Highlights | None = None
    next_week: NextPlan | None = None
    methods: list[MethodUsed] = Field(default_factory=list)


class Consistency(_Model):
    """Monthly section 2: sessions planned / done / achieved per week, longest gap."""

    planned: int
    done: int
    achieved: int
    by_week: list[tuple[dt.date, int, int, int]] = Field(
        default_factory=list, description="(week start, planned, done, achieved)"
    )
    longest_gap_days: int = 0
    text_zh: str = ""


class PowerCurve(_Model):
    """Monthly section 4: best power by duration, this month vs last month and season best."""

    durations_s: list[int]
    this_month_w: list[float | None]
    last_month_w: list[float | None]
    season_best_w: list[float | None]
    text_zh: str = ""


class FitnessModel(_Model):
    """Monthly section 5: FTP / CP / W' over the month and what backs them."""

    ftp_start_w: float | None = None
    ftp_end_w: float | None = None
    cp_w: float | None = None
    w_prime_j: float | None = None
    backing_efforts_zh: list[str] = Field(default_factory=list)
    goal: GoalProgress | None = None
    text_zh: str = ""
    explanation: dict[str, Any] | None = None


class LimiterRank(_Model):
    """One limiter's rank now and a month ago."""

    limiter_id: str
    name_zh: str
    rank_now: int | None = None
    rank_before: int | None = None


class LimiterShift(_Model):
    """Monthly section 7: limiter ranking now vs a month ago, and what the plan does about it."""

    lines: list[LimiterRank] = Field(default_factory=list)
    plan_response_zh: str = ""


class ResearchNote(_Model):
    """Monthly section 10: the literature review that landed this month."""

    report_path: str
    approved_changes_zh: list[str] = Field(default_factory=list)
    grade_changes: list[tuple[str, Grade, Grade]] = Field(
        default_factory=list, description="(claim id, before, after)"
    )


class MonthlyDigest(_Model):
    """Everything the monthly PDF shows."""

    version: str = MONTHLY_VERSION
    profile: str
    athlete_name: str
    generated_at: dt.datetime
    period: Period
    headline: DigestHeadline
    consistency: Consistency | None = None
    load: Load | None = None
    power_curve: PowerCurve | None = None
    fitness: FitnessModel | None = None
    distribution_by_week: list[tuple[dt.date, Distribution]] = Field(default_factory=list)
    limiters: LimiterShift | None = None
    recovery: Recovery | None = None
    next_month: NextPlan | None = None
    research: ResearchNote | None = None
    methods: list[MethodUsed] = Field(default_factory=list)
