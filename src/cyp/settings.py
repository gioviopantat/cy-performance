"""Runtime settings (.env via pydantic-settings) and the git-tracked athlete config (YAML).

Secrets and machine-specific paths come from the environment / ``.env``; the athlete's intent
(goals, season, availability, planner knobs) comes from ``config/athlete.yaml``.
See docs/01-architecture.md §7.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cyp.core.errors import ConfigError

DEFAULT_ATHLETE_CONFIG = Path("config/athlete.yaml")

NonNegInt = Annotated[int, Field(ge=0)]
NonNegFloat = Annotated[float, Field(ge=0)]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class Settings(BaseSettings):
    """Process-level configuration read from the environment and ``.env``.

    Field names mirror ``.env.example``. Secrets are ``SecretStr`` so they never print.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Strava
    strava_enabled: bool = True
    strava_client_id: str = ""
    strava_client_secret: SecretStr = SecretStr("")
    strava_redirect_uri: str = "http://localhost:8721/callback"
    strava_scope: str = "activity:read_all,profile:read_all"
    strava_webhook_verify_token: SecretStr = SecretStr("")
    #: Max Strava detail + fallback-stream fetches per run (rate budget; docs/02 §2.2).
    strava_max_detail_fetches: int = Field(default=60, ge=0)

    # intervals.icu
    intervals_api_key: SecretStr = SecretStr("")
    intervals_athlete_id: str = "0"

    # Storage
    cyp_data_dir: Path = Path("data")
    cyp_db_url: str = "sqlite:///data/cyp.sqlite"

    # Planner
    cyp_plan_horizon_days: int = Field(default=14, ge=1)
    cyp_plan_mode: Literal["propose", "apply"] = "propose"
    cyp_timezone: str = "Asia/Taipei"

    # HTTP API (cyp serve)
    cyp_api_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    cyp_api_token: SecretStr = SecretStr("")

    @property
    def api_cors_origins(self) -> list[str]:
        """``CYP_API_CORS_ORIGINS`` split on commas."""
        return [o.strip() for o in self.cyp_api_cors_origins.split(",") if o.strip()]

    # Optional LLM
    anthropic_api_key: SecretStr = SecretStr("")
    cyp_llm_model: str = "claude-sonnet-5-5"
    cyp_llm_enabled: bool = False

    @field_validator("cyp_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on tzdata
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    # Derived paths -----------------------------------------------------------------
    @property
    def streams_dir(self) -> Path:
        """Directory holding one Parquet file per activity."""
        return self.cyp_data_dir / "streams"

    @property
    def tokens_dir(self) -> Path:
        """Directory holding OAuth token files (mode 600, never in git)."""
        return self.cyp_data_dir / "tokens"

    @property
    def logs_dir(self) -> Path:
        """Directory holding structlog JSON lines."""
        return self.cyp_data_dir / "logs"

    @property
    def reports_dir(self) -> Path:
        """Directory holding rendered daily/weekly reports."""
        return self.cyp_data_dir / "reports"

    @property
    def data_layout(self) -> tuple[Path, ...]:
        """Every directory ``cyp init`` creates."""
        return (
            self.cyp_data_dir,
            self.streams_dir,
            self.tokens_dir,
            self.logs_dir,
            self.reports_dir,
        )

    def masked_summary(self) -> dict[str, str]:
        """Settings as printable strings with secrets masked (for ``cyp doctor``)."""
        out: dict[str, str] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                out[name] = "***" if value.get_secret_value() else "(unset)"
            else:
                out[name] = str(value)
        return out


def get_settings(**overrides: object) -> Settings:
    """Build ``Settings`` from the environment; keyword overrides win (handy in tests)."""
    return Settings(**overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Athlete config (config/athlete.yaml)
# --------------------------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AthleteBasics(_StrictModel):
    """Season-baseline physiology; runtime values come from icu sport settings."""

    weight_kg: Annotated[float, Field(gt=0)]
    ftp_w: Annotated[int, Field(gt=0)]


class GoalCheckpoint(_StrictModel):
    """Expected FTP at a given season week; used for reports, not plan inputs."""

    week: Annotated[int, Field(ge=0)]
    ftp: Annotated[int, Field(gt=0)]


class Goal(_StrictModel):
    """A goal event or target (mirrors the ``goals`` table)."""

    name: str
    date: dt.date
    category: Literal["RACE_A", "RACE_B", "RACE_C", "TARGET"]
    kind: Literal["climb_tt", "gran_fondo", "crit", "ftp_target", "wkg_target"]
    target: dict[str, float] = Field(default_factory=dict)
    checkpoints: list[GoalCheckpoint] = Field(default_factory=list)
    icu_event_id: int | None = None
    priority: int | None = None
    notes: str | None = None


class TestCadence(_StrictModel):
    """Repeat a test every N weeks starting at ``offset_week``."""

    every_weeks: Annotated[int, Field(ge=1)]
    offset_week: Annotated[int, Field(ge=0)]


class SeasonTests(_StrictModel):
    """Which fitness tests the season schedules and how often."""

    ramp: TestCadence | None = None
    twenty_min: TestCadence | None = None


class SeasonConfig(_StrictModel):
    """Macro season definition."""

    start: dt.date
    type: Literal["ftp_target", "race", "wkg_target", "general"]
    load_pattern: Literal["3:1", "2:1"] = "3:1"
    tests: SeasonTests = Field(default_factory=SeasonTests)


class Availability(_StrictModel):
    """Minutes available per weekday plus a weekly ceiling.

    Per-day caps are independent of ``weekly_max_minutes``; the planner applies the weekly
    ceiling first, then the per-day caps.
    """

    weekly_max_minutes: NonNegInt
    mon: NonNegInt = 0
    tue: NonNegInt = 0
    wed: NonNegInt = 0
    thu: NonNegInt = 0
    fri: NonNegInt = 0
    sat: NonNegInt = 0
    sun: NonNegInt = 0

    def minutes_for(self, day: Weekday) -> int:
        """Available minutes for a weekday key (``mon``..``sun``)."""
        return int(getattr(self, day))

    @property
    def per_day(self) -> dict[Weekday, int]:
        """Mapping weekday -> minutes."""
        return {d: self.minutes_for(d) for d in WEEKDAYS}


class WeatherIndoorIf(_StrictModel):
    """Weather thresholds beyond which an outdoor ride is moved indoors."""

    rain_prob_pct: Annotated[float, Field(ge=0, le=100)] = 60
    temp_c_min: float = 8
    temp_c_max: float = 36

    @model_validator(mode="after")
    def _ordered(self) -> WeatherIndoorIf:
        if self.temp_c_min >= self.temp_c_max:
            raise ValueError("temp_c_min must be below temp_c_max")
        return self


class LocationConfig(_StrictModel):
    """Indoor/outdoor preferences."""

    indoor_policy: Literal["fallback", "always", "never"] = "fallback"
    indoor_platform: str | None = None
    weather_indoor_if: WeatherIndoorIf = Field(default_factory=WeatherIndoorIf)
    test_venue: Literal["indoor", "outdoor"] = "indoor"


class DataQualityConfig(_StrictModel):
    """Known problems in the athlete's historical data."""

    #: Last day whose power files were recorded with "include zeros" off (empty power while
    #: coasting). Up to and including this day the daily load uses our re-analysed TSS (empty
    #: power = 0 W) instead of intervals.icu's load, and the PMC agreement check starts after it.
    power_zeros_excluded_until: dt.date | None = None


class OtherSportsConfig(_StrictModel):
    """How non-cycling activities (strength, yoga) are treated."""

    plan: bool = False
    ingest_load: bool = True
    hit_downgrade_after_strength: bool = True


Phase = Literal["base", "build", "threshold", "test"]


class PlannerConfig(_StrictModel):
    """Planner knobs (docs/05-training-engine.md). Phase-keyed maps may be partial."""

    horizon_days: Annotated[int, Field(ge=1, le=56)] = 14
    mode: Literal["propose", "apply"] = "propose"
    today_cutoff_local: dt.time = dt.time(10, 0)
    ramp_cap: dict[str, NonNegFloat] = Field(default_factory=dict)
    tsb_floor: dict[str, float] = Field(default_factory=dict)
    hit_per_week: dict[str, NonNegInt] = Field(default_factory=dict)
    tid_model: Literal["pyramidal", "polarized", "threshold"] = "pyramidal"
    long_ride_day: Weekday = "sat"

    @field_validator("today_cutoff_local", mode="before")
    @classmethod
    def _parse_time(cls, value: object) -> object:
        if isinstance(value, str):
            return dt.time.fromisoformat(value)
        return value


class AthleteConfig(_StrictModel):
    """Root of ``config/athlete.yaml``."""

    timezone: str = "Asia/Taipei"
    athlete: AthleteBasics
    goals: list[Goal] = Field(min_length=1)
    season: SeasonConfig
    availability: Availability
    location: LocationConfig = Field(default_factory=LocationConfig)
    other_sports: OtherSportsConfig = Field(default_factory=OtherSportsConfig)
    data_quality: DataQualityConfig = Field(default_factory=DataQualityConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)

    @model_validator(mode="after")
    def _goals_after_season_start(self) -> AthleteConfig:
        for goal in self.goals:
            if goal.date < self.season.start:
                raise ValueError(
                    f"goal {goal.name!r} dated {goal.date} precedes season start "
                    f"{self.season.start}"
                )
        return self


def load_athlete_config(path: Path | str = DEFAULT_ATHLETE_CONFIG) -> AthleteConfig:
    """Parse and validate ``config/athlete.yaml``.

    Raises:
        ConfigError: file missing, not a mapping, or fails validation.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"athlete config not found: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p} must contain a mapping at the top level")
    try:
        return AthleteConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"invalid athlete config {p}:\n{exc}") from exc
