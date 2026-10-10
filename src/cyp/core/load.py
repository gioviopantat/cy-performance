"""Training load, fitness state and readiness (docs/03 ``fitness_daily`` / ``readiness_daily``)."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cyp.core.explain import Explanation

LoadSource = Literal["power", "hr", "estimated", "icu"]
ReadinessStatus = Literal["FRESH", "NORMAL", "BLUNTED", "OVERREACHED", "SICK"]
Recommendation = Literal["REST", "EASY", "AS_PLANNED", "UPGRADE"]


class TrainingLoad(BaseModel):
    """One session's load (TSS-like) with the method used to derive it."""

    model_config = ConfigDict(extra="forbid")

    date_local: dt.date
    tss: float = Field(ge=0)
    source: LoadSource
    duration_s: int = Field(ge=0)
    intensity: float | None = Field(default=None, ge=0)
    activity_id: int | None = None


class FitnessState(BaseModel):
    """Banister PMC state on a day. ``*_icu`` copied from icu, ``*_sim`` our replay."""

    model_config = ConfigDict(extra="forbid")

    date_local: dt.date
    ctl: float = Field(ge=0)
    atl: float = Field(ge=0)
    tsb: float
    ramp_rate: float | None = None
    source: Literal["icu", "sim"] = "icu"
    load_actual: float | None = None
    load_planned: float | None = None
    acwr_7_28: float | None = None
    monotony_7: float | None = None
    strain_7: float | None = None


class Readiness(BaseModel):
    """Daily readiness verdict fused from wellness and yesterday's response."""

    model_config = ConfigDict(extra="forbid")

    date_local: dt.date
    score_0_100: float = Field(ge=0, le=100)
    status: ReadinessStatus
    recommendation: Recommendation
    inputs: dict[str, Any] = Field(default_factory=dict)
    explanation: Explanation | None = None
    algo_version: str = "readiness_v1"
