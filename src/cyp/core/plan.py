"""Plan domain: season → blocks → week templates → planned workouts (docs/03, docs/05)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cyp.core.explain import Explanation

Phase = Literal["base", "build", "specialty", "peak", "taper", "recovery", "transition"]
Intent = Literal[
    "endurance",
    "tempo",
    "sweetspot",
    "threshold",
    "vo2",
    "anaerobic",
    "recovery",
    "test",
    "race",
]
StepKind = Literal["warmup", "work", "recovery", "cooldown", "steady", "ramp", "freeride"]
WorkoutStatus = Literal["proposed", "published", "completed", "skipped", "modified", "superseded"]
TargetMode = Literal["POWER", "HR"]


class WorkoutStep(BaseModel):
    """One step of a structured workout; targets are % of FTP (or % LTHR in HR mode)."""

    model_config = ConfigDict(extra="forbid")

    kind: StepKind
    duration_s: int = Field(gt=0)
    target_pct_lo: float = Field(ge=0)
    target_pct_hi: float = Field(ge=0)
    cadence_rpm: int | None = Field(default=None, gt=0)
    repeat: int = Field(default=1, ge=1)
    note: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> WorkoutStep:
        if self.target_pct_hi < self.target_pct_lo:
            raise ValueError("target_pct_hi must be >= target_pct_lo")
        return self


class PlannedWorkout(BaseModel):
    """A workout on a date; ``external_id`` is the idempotency key on intervals.icu."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    week_plan_id: int | None = None
    date_local: dt.date
    slot: int = Field(default=1, ge=1)
    external_id: str
    template_id: str | None = None
    template_version: str | None = None
    name: str
    intent: Intent
    steps: list[WorkoutStep] = Field(default_factory=list)
    workout_text: str | None = None
    target_tss: float | None = Field(default=None, ge=0)
    target_duration_s: int | None = Field(default=None, ge=0)
    target_if: float | None = Field(default=None, ge=0)
    indoor: bool = False
    target_mode: TargetMode = "POWER"
    status: WorkoutStatus = "proposed"
    icu_event_id: int | None = None
    created_by: Literal["planner", "llm", "athlete"] = "planner"
    revision: int = Field(default=1, ge=1)
    explanation: Explanation | None = None

    @staticmethod
    def make_external_id(season: str | int, date_local: dt.date, slot: int) -> str:
        """``cyp:{season}:{date}:{slot}`` (docs/03 ``planned_workouts``)."""
        return f"cyp:{season}:{date_local.isoformat()}:{slot}"


class WeekTemplate(BaseModel):
    """Weekly targets inside a block (``week_plans``)."""

    model_config = ConfigDict(extra="forbid")

    week_start: dt.date
    target_tss: float = Field(ge=0)
    target_hours: float = Field(ge=0)
    hit_sessions: int = Field(ge=0)
    planned_ctl_end: float | None = None
    template_id: str | None = None
    status: Literal["draft", "published", "completed"] = "draft"
    notes: str | None = None


class Block(BaseModel):
    """A mesocycle (``blocks``)."""

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=0)
    phase: Phase
    start_date: dt.date
    end_date: dt.date
    weeks: int = Field(ge=1)
    load_pattern: Literal["3:1", "2:1"] = "3:1"
    focus: dict[str, Any] = Field(default_factory=dict)
    week_templates: list[WeekTemplate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates(self) -> Block:
        if self.end_date < self.start_date:
            raise ValueError("block end_date before start_date")
        return self


class Season(BaseModel):
    """A goal-driven macro season (``seasons``)."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    name: str
    start_date: dt.date
    end_date: dt.date
    goal_id: int | None = None
    status: Literal["active", "archived"] = "active"
    config: dict[str, Any] = Field(default_factory=dict)
    blocks: list[Block] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates(self) -> Season:
        if self.end_date < self.start_date:
            raise ValueError("season end_date before start_date")
        return self
