"""Athlete physiology models: profile, zones, settings history.

Mirrors docs/03 ``athlete_settings_history``.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ZoneKind = Literal["power", "hr"]


class Zone(BaseModel):
    """One training zone bounded by absolute values (W or bpm); ``hi`` ``None`` = open-ended."""

    model_config = ConfigDict(extra="forbid")

    idx: int = Field(ge=1)
    name: str
    lo: float = Field(ge=0)
    hi: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Zone:
        if self.hi is not None and self.hi < self.lo:
            raise ValueError(f"zone {self.name}: hi < lo")
        return self

    def contains(self, value: float) -> bool:
        """True if ``value`` falls in ``[lo, hi)``."""
        return value >= self.lo and (self.hi is None or value < self.hi)


class ZoneModel(BaseModel):
    """An ordered set of zones for power or HR, anchored on FTP/LTHR."""

    model_config = ConfigDict(extra="forbid")

    kind: ZoneKind
    anchor: float = Field(gt=0, description="FTP (W) or LTHR (bpm)")
    zones: list[Zone] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous(self) -> ZoneModel:
        idxs = [z.idx for z in self.zones]
        if idxs != sorted(idxs) or len(set(idxs)) != len(idxs):
            raise ValueError("zones must have unique ascending idx")
        return self

    def zone_for(self, value: float) -> Zone | None:
        """Return the zone containing ``value``, if any."""
        for z in self.zones:
            if z.contains(value):
                return z
        return None


class AthleteProfile(BaseModel):
    """Current physiological model of the athlete (docs/01 §5.1)."""

    model_config = ConfigDict(extra="forbid")

    athlete_id: int | None = None
    as_of: dt.date | None = None
    weight_kg: float = Field(gt=0)
    ftp_w: float = Field(gt=0)
    indoor_ftp_w: float | None = Field(default=None, gt=0)
    eftp_w: float | None = Field(default=None, gt=0)
    lthr: int | None = Field(default=None, gt=0)
    max_hr: int | None = Field(default=None, gt=0)
    resting_hr: int | None = Field(default=None, gt=0)
    cp_w: float | None = Field(default=None, gt=0)
    w_prime_j: float | None = Field(default=None, gt=0)
    p_max_w: float | None = Field(default=None, gt=0)
    power_zones: ZoneModel | None = None
    hr_zones: ZoneModel | None = None
    timezone: str = "Asia/Taipei"

    @property
    def w_kg(self) -> float:
        """FTP in W/kg."""
        return self.ftp_w / self.weight_kg


class AthleteSettingsSnapshot(BaseModel):
    """A row of ``athlete_settings_history``: settings effective from a date."""

    model_config = ConfigDict(extra="forbid")

    effective_from: dt.date
    source: Literal["icu_sport_settings", "icu_eftp", "manual"]
    ftp: float | None = None
    indoor_ftp: float | None = None
    eftp: float | None = None
    w_prime: float | None = None
    p_max: float | None = None
    lthr: int | None = None
    max_hr: int | None = None
    resting_hr: int | None = None
    weight_kg: float | None = None
    power_zones: ZoneModel | None = None
    hr_zones: ZoneModel | None = None
