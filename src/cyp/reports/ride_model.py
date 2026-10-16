"""Data contract of the per-ride report (docs/specs/ride-report.md).

``reports.ride_facts`` fills a :class:`RideReport`; ``reports.ride_pdf`` renders it; nothing in
between computes. Every number a section shows is a field here, so the PDF never invents
figures (docs/07). Section numbers match the spec table. Optional sections are ``None`` when
their inputs are missing; the renderer then leaves the section out.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

REPORT_VERSION = "ride_report_v1"

Verdict = Literal["achieved", "partial", "missed", "unplanned", "no_data"]
Grade = Literal["A", "B", "C"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RideSummary(_Model):
    """Header block: what the ride was."""

    activity_id: int
    name: str
    start_local: dt.datetime
    sport: str
    indoor: bool
    moving_min: int
    distance_km: float | None = None
    elev_gain_m: int | None = None
    np_w: int | None = None
    if_: float | None = None
    tss: int | None = None
    tss_source: str | None = None


class Headline(_Model):
    """Section 1: one-line verdict."""

    text_zh: str
    verdict: Verdict


class Segment(_Model):
    """A stretch of the ride on the time axis (seconds from start)."""

    start_s: int
    end_s: int
    label_zh: str = ""


class Intent(_Model):
    """Section 2: planned stimulus vs achieved."""

    planned_name: str
    #: How the stimulus is judged: ``work`` (minutes in the band) or ``ceiling`` (stay under it).
    kind: Literal["work", "ceiling"]
    purpose_zh: str
    target_zone: str
    target_low_w: int | None = None
    target_high_w: int | None = None
    target_min: float
    achieved_min: float
    reps_planned: int | None = None
    reps_counted: int | None = None
    load_planned: float | None = None
    load_actual: float | None = None
    load_ratio: float | None = None
    counted: list[Segment] = Field(default_factory=list)
    excused: list[Segment] = Field(default_factory=list)
    verdict: Verdict
    explanation: dict[str, Any] | None = None


class FtpPoint(_Model):
    """One FTP estimate on the goal trajectory."""

    day: dt.date
    ftp_w: float
    #: ``setting:<origin>`` for an FTP setting, else the estimate's id (e.g. ``icu_eftp``).
    #: Charts draw settings and estimates as separate series.
    source: str


class Goal(_Model):
    """Section 3: distance to the season FTP target."""

    target_w: float
    target_date: dt.date
    current_ftp_w: float
    current_source: str
    #: Today's value of the fitted trend; the projection line starts here, not at the setting.
    trend_level_w: float | None = None
    this_ride_evidence_w: float | None = None
    projected_date: dt.date | None = None
    projected_shift_days: int | None = None
    on_track: bool | None = None
    history: list[FtpPoint] = Field(default_factory=list)
    explanation: dict[str, Any] | None = None


class OutdoorContext(_Model):
    """Section 4: outdoor context (stops, coasting, heat)."""

    stops_n: int
    stopped_min: float
    #: Percent of moving time at 0 W, 0-100 (not a fraction).
    coasting_pct: float | None = None
    temp_avg_c: float | None = None
    temp_max_c: float | None = None
    stops: list[Segment] = Field(default_factory=list)
    caveats_zh: list[str] = Field(default_factory=list)


class ClimbEffort(_Model):
    """One ascent of a climb, with the fatigue and heat it was ridden in."""

    day: dt.date
    power_w: float | None = None
    w_kg: float | None = None
    time_s: int
    kj_before: float | None = None
    temp_c: float | None = None


class ClimbCompare(_Model):
    """Section 5: this climb vs its history (one per named climb on this ride)."""

    name: str
    this: ClimbEffort
    history: list[ClimbEffort] = Field(default_factory=list)
    rank: int | None = None
    of_n: int | None = None
    note_zh: str = ""


class Feel(_Model):
    """Section 6: perceived effort vs what the data predicts."""

    rated: bool
    rpe: float | None = None
    feel: int | None = None
    expected_rpe: float | None = None
    gap: float | None = None
    text_zh: str = ""
    rate_url: str | None = None


class LimiterLine(_Model):
    """Minutes this ride (and this week) spent on one limiter."""

    limiter_id: str
    name_zh: str
    trained_min: float
    week_min: float


class Limiters(_Model):
    """Section 7: limiter ledger."""

    lines: list[LimiterLine] = Field(default_factory=list)
    text_zh: str = ""


class Tomorrow(_Model):
    """Section 8: tomorrow's plan (dry-run preview; never written)."""

    day: dt.date
    before_name: str | None = None
    after_name: str | None = None
    changed: bool
    reasons_zh: list[str] = Field(default_factory=list)


class MethodUsed(_Model):
    """Section 9: methods and evidence; one model a number above came from."""

    model_id: str
    claim_id: str | None = None
    grade: Grade | None = None
    citation: str | None = None
    doc: str


class Series(_Model):
    """Downsampled streams for the charts (equal-length lists, ``step_s`` apart)."""

    step_s: int
    power_w: list[float | None] = Field(default_factory=list)
    hr_bpm: list[float | None] = Field(default_factory=list)
    altitude_m: list[float | None] = Field(default_factory=list)
    temp_c: list[float | None] = Field(default_factory=list)


class RideReport(_Model):
    """Everything one ride's PDF shows."""

    version: str = REPORT_VERSION
    profile: str
    athlete_name: str
    #: Timezone-aware; renderers convert it to the athlete's local time.
    generated_at: dt.datetime
    ride: RideSummary
    headline: Headline
    intent: Intent | None = None
    goal: Goal | None = None
    outdoor: OutdoorContext | None = None
    climbs: list[ClimbCompare] = Field(default_factory=list)
    feel: Feel
    limiters: Limiters | None = None
    tomorrow: Tomorrow | None = None
    methods: list[MethodUsed] = Field(default_factory=list)
    series: Series | None = None
