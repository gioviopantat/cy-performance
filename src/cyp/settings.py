"""Runtime settings (.env via pydantic-settings) and the athlete config (YAML).

Secrets and machine-specific paths come from the environment / ``.env``; the athlete's intent
(goals, season, availability, planner knobs, feature flags) comes from ``athlete.yaml``.

With a profile selected (``--profile`` / ``CYP_PROFILE`` / ``profiles/.default``; ADR-0006),
everything comes from ``profiles/<slug>/``: its ``.env`` (the root ``.env`` is NOT read, so one
athlete's key can never leak into another's run), its ``athlete.yaml`` and its ``data/``.
Without a profile the legacy layout (``.env``, ``config/athlete.yaml``, ``data/``) applies.
See docs/01-architecture.md §7.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cyp.core import features as feature_flags
from cyp.core.data_quality import DataQuality, PowerRule, normalise_serial
from cyp.core.errors import ConfigError

DEFAULT_ATHLETE_CONFIG = Path("config/athlete.yaml")

# Profile layout (ADR-0006). ``cyp.profiles`` builds on these; keep them the single source.
DEFAULT_PROFILES_DIR = Path("profiles")
PROFILE_ENV = ".env"
PROFILE_ATHLETE = "athlete.yaml"
PROFILE_META = "profile.yaml"
PROFILE_DATA = "data"
PROFILE_DEFAULT_FILE = ".default"
#: Process-environment settings a profile run may still take (everything else comes only from
#: the profile's ``.env`` or the default): one-off flag overrides and where profiles live.
PROFILE_INHERITABLE = ("cyp_features", "cyp_profiles_dir")
#: Settings of the server process, not of an athlete: always taken from the process environment
#: and the root ``.env`` (``cyp serve`` serves every profile with one token and one CORS list).
SERVER_SETTINGS = ("cyp_api_token", "cyp_api_cors_origins")
#: Valid profile names (also used by :mod:`cyp.profiles`).
SLUG_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"

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
    cyp_athlete_config: Path = DEFAULT_ATHLETE_CONFIG

    # Profiles (ADR-0006) and feature flags (ADR-0008)
    #: Selected profile slug; empty = legacy single-athlete layout.
    cyp_profile: str = ""
    cyp_profiles_dir: Path = DEFAULT_PROFILES_DIR
    #: One-off feature overrides, e.g. ``publish.calendar=off,notify.macos=on``.
    cyp_features: str = ""

    # Planner
    cyp_plan_horizon_days: int = Field(default=14, ge=1)
    #: DEPRECATED, ignored: the write mode is ``athlete.yaml`` ``planner.mode`` (ADR-0006).
    cyp_plan_mode: Literal["propose", "apply"] = "propose"
    cyp_timezone: str = "Asia/Taipei"

    # HTTP API (cyp serve)
    cyp_api_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    cyp_api_token: SecretStr = SecretStr("")

    @property
    def api_cors_origins(self) -> list[str]:
        """``CYP_API_CORS_ORIGINS`` split on commas."""
        return [o.strip() for o in self.cyp_api_cors_origins.split(",") if o.strip()]

    # Optional LLM (not used yet; a narrator will ship behind a feature flag, ADR-0008)
    anthropic_api_key: SecretStr = SecretStr("")
    cyp_llm_model: str = "claude-sonnet-5-5"

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
    """Build ``Settings`` for the selected profile (or the legacy layout).

    Keyword overrides win (handy in tests). The profile comes from ``cyp_profile``
    (``--profile`` / ``CYP_PROFILE``), else ``<profiles_dir>/.default``.

    Raises:
        ConfigError: the selected profile directory does not exist.
    """
    base = Settings(**overrides)  # type: ignore[arg-type]
    slug = base.cyp_profile or read_default_profile(base.cyp_profiles_dir)
    if not slug:
        return base
    server = {name: getattr(base, name) for name in SERVER_SETTINGS}
    return profile_settings(slug, base.cyp_profiles_dir, **{**server, **overrides})


def read_default_profile(profiles_dir: Path) -> str:
    """Slug in ``<profiles_dir>/.default`` or ``""``."""
    marker = profiles_dir / PROFILE_DEFAULT_FILE
    return marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""


def profile_settings(slug: str, profiles_dir: Path, **overrides: object) -> Settings:
    """Settings for ``profiles_dir/slug``: its ``.env`` only, paths forced inside the profile.

    Hermetic: a field missing from the profile's ``.env`` takes its default, never a value from
    the process environment or the root ``.env`` (one athlete's key, time zone or Strava switch
    cannot leak into another's run); ``${VAR}`` is not expanded. Exceptions:
    :data:`PROFILE_INHERITABLE`, and :data:`SERVER_SETTINGS` which :func:`get_settings` passes in.

    Raises:
        ConfigError: invalid name or no such profile directory.
    """
    import os
    import re

    from dotenv import dotenv_values

    if not re.match(SLUG_PATTERN, slug):
        raise ConfigError(f"invalid profile name {slug!r} (lowercase letters, digits, '-')")
    root = profiles_dir / slug
    if not root.is_dir():
        raise ConfigError(f"profile {slug!r} not found in {profiles_dir}/ (see `cyp profile list`)")
    fields = Settings.model_fields
    values: dict[str, object] = {
        k.lower(): v
        for k, v in dotenv_values(root / PROFILE_ENV, interpolate=False).items()
        if v is not None and k.lower() in fields
    }
    for name, info in fields.items():
        if name in values:
            continue
        if name in PROFILE_INHERITABLE and name.upper() in os.environ:
            values[name] = os.environ[name.upper()]
        else:
            values[name] = info.default
    data_dir = (root / PROFILE_DATA).resolve()
    values.update(
        cyp_profile=slug,
        cyp_profiles_dir=profiles_dir,
        cyp_data_dir=data_dir,
        cyp_db_url=f"sqlite:///{data_dir / 'cyp.sqlite'}",
        cyp_athlete_config=root / PROFILE_ATHLETE,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type,call-arg]


# --------------------------------------------------------------------------------------
# Athlete config (config/athlete.yaml)
# --------------------------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


HealthFlag = Literal["heart_or_bp", "symptoms_on_exertion", "hr_medication", "injury"]


class AthleteBasics(_StrictModel):
    """Season-baseline physiology; runtime values come from icu sport settings."""

    weight_kg: Annotated[float, Field(gt=0)]
    ftp_w: Annotated[int, Field(gt=0)]
    #: Onboarding question 1; drives age defaults (``planning.athlete_rules``).
    birth_year: Annotated[int, Field(ge=1900, le=2030)] | None = None
    #: Onboarding question 2: ``[]`` = screened, no issues; ``None`` = not screened.
    health_flags: list[HealthFlag] | None = None


class GoalCheckpoint(_StrictModel):
    """Expected FTP at a given season week; used for reports, not plan inputs."""

    week: Annotated[int, Field(ge=0)]
    ftp: Annotated[int, Field(gt=0)]


class Goal(_StrictModel):
    """A goal event or target (mirrors the ``goals`` table)."""

    name: str
    date: dt.date
    category: Literal["RACE_A", "RACE_B", "RACE_C", "TARGET"]
    kind: Literal["climb_tt", "gran_fondo", "crit", "ftp_target", "wkg_target", "distance"]
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
    #: The 20-min test in the last season week (off for athletes who must avoid maximal tests).
    final_test: bool = True


class Availability(_StrictModel):
    """Minutes available per weekday plus a weekly ceiling.

    Per-day caps are independent of ``weekly_max_minutes``; the planner applies the weekly
    ceiling first, then the per-day caps.
    """

    weekly_max_minutes: NonNegInt
    #: Ceiling the long-ride progression may grow to (needs a ``distance`` goal).
    long_ride_max_minutes: Annotated[int, Field(ge=60, le=600)] | None = None
    mon: NonNegInt = 0
    tue: NonNegInt = 0
    wed: NonNegInt = 0
    thu: NonNegInt = 0
    fri: NonNegInt = 0
    sat: NonNegInt = 0
    sun: NonNegInt = 0
    #: One-off minutes for single dates (e.g. a swapped weekend); replaces the weekday's minutes.
    #: When the long-ride day gets too little, the long ride moves to the week's longest day.
    dates: dict[dt.date, NonNegInt] = Field(default_factory=dict)

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


ClimbUse = Literal["endurance", "tempo", "sweetspot", "threshold", "vo2", "test"]


class ClimbConfig(_StrictModel):
    """A local climb the planner may suggest for long outdoor work steps."""

    name_zh: str
    minutes_min: Annotated[float, Field(gt=0)]
    minutes_max: Annotated[float, Field(gt=0)]
    distance_km: Annotated[float, Field(gt=0)]
    grade_pct: float
    good_for: list[ClimbUse] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered(self) -> ClimbConfig:
        if self.minutes_min > self.minutes_max:
            raise ValueError(f"climb {self.name_zh!r}: minutes_min > minutes_max")
        return self


class LocationConfig(_StrictModel):
    """Indoor/outdoor preferences."""

    indoor_policy: Literal["fallback", "always", "never"] = "fallback"
    indoor_platform: str | None = None
    weather_indoor_if: WeatherIndoorIf = Field(default_factory=WeatherIndoorIf)
    test_venue: Literal["indoor", "outdoor"] = "indoor"
    #: Local climbs for outdoor work steps ≥ 8 min (planning/routes.py).
    climbs: list[ClimbConfig] = Field(default_factory=list)


class PowerUnreliableEntry(_StrictModel):
    """One ``data_quality.power_unreliable`` entry (docs/04 §7)."""

    #: Last day (inclusive) whose power from this meter is not trusted.
    until: dt.date
    #: icu ``power_meter_serial``; ``null`` = every power meter.
    power_meter_serial: str | None = None
    #: zh-TW reason, quoted in ride Explanations.
    reason_zh: str = ""

    @field_validator("power_meter_serial", mode="before")
    @classmethod
    def _serial_text(cls, value: object) -> object:
        # YAML parses an unquoted serial as an int; icu stores it as text.
        return normalise_serial(value) if value is not None else None


class DataQualityConfig(_StrictModel):
    """Known problems in the athlete's historical data."""

    #: Power meters whose power is unreliable up to a date (see ``cyp.core.data_quality``).
    #: Matching rides keep HR / time / elevation but are excluded from every power model.
    power_unreliable: list[PowerUnreliableEntry] = Field(default_factory=list)
    #: DEPRECATED alias: equals one ``power_unreliable`` entry with ``power_meter_serial: null``
    #: (every meter). Last day whose power files were recorded with "include zeros" off.
    power_zeros_excluded_until: dt.date | None = None

    @property
    def entries(self) -> list[PowerUnreliableEntry]:
        """``power_unreliable`` plus the deprecated alias (as a serial-less entry)."""
        out = list(self.power_unreliable)
        if self.power_zeros_excluded_until is not None:
            out.append(
                PowerUnreliableEntry(
                    until=self.power_zeros_excluded_until,
                    power_meter_serial=None,
                    reason_zh="碼表「包含零值」關閉，滑行時功率空白（power_zeros_excluded_until）",
                )
            )
        return out

    @property
    def load_fix_until(self) -> dt.date | None:
        """Up to this day the daily load uses our re-analysed TSS (latest entry's ``until``).

        icu's daily loads for those days were computed from the same unreliable files, so the
        PMC is seeded and compared only after it.
        """
        return max((e.until for e in self.entries), default=None)

    def resolved(self) -> DataQuality:
        """Pure, hashable view used by the dataset and the analysis."""
        return DataQuality(
            power_rules=tuple(
                PowerRule(e.until, e.power_meter_serial, e.reason_zh) for e in self.entries
            ),
            load_fix_until=self.load_fix_until,
        )


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
    #: Preferred weekdays for hard sessions, in order (``None`` = Tue, Thu, Wed, Fri, ...).
    hit_days: list[Weekday] | None = None
    #: Onboarding question 6: shifts template alternatives towards tempo (easy) or VO2 (hard).
    intensity: Literal["easy", "moderate", "hard"] = "moderate"
    #: ``phase`` = one menu per phase (v1); ``goal`` = menus by goal emphasis (spec
    #: personalized-planning). Set by ``plan.goal_menus``; written values win.
    menus: Literal["phase", "goal"] = "phase"
    #: Rotate equally ranked alternatives by week. Set by ``plan.variety``.
    variety: bool = False

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
    #: Feature flags for this athlete (``cyp.core.features``; unknown ids are an error).
    features: dict[str, bool] = Field(default_factory=dict)

    @field_validator("features")
    @classmethod
    def _known_features(cls, value: dict[str, bool]) -> dict[str, bool]:
        try:
            feature_flags.check_ids(value, where="features")
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc
        return value

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


def resolve_features(settings: Settings, cfg: AthleteConfig | None) -> feature_flags.FeatureSet:
    """Feature flags for this run: registry defaults, legacy env, ``athlete.yaml``, env override.

    ``STRAVA_ENABLED=false`` keeps working as a legacy switch for ``sync.strava``.

    Raises:
        ConfigError: unknown ids in ``CYP_FEATURES``.
    """
    legacy = {} if settings.strava_enabled else {"sync.strava": False}
    return feature_flags.resolve(
        config=cfg.features if cfg else None, env=settings.cyp_features, legacy=legacy
    )


def with_feature_switches(settings: Settings) -> Settings:
    """``settings`` with flag-driven fields applied (``strava_enabled`` <- ``sync.strava``).

    Lower layers (ingest, jobs) keep reading plain settings; this is the one bridge from flags
    to them. Idempotent. A missing/invalid athlete.yaml only means "no profile flags".
    """
    try:
        cfg: AthleteConfig | None = load_athlete_config(settings.cyp_athlete_config)
    except ConfigError:
        cfg = None
    strava = resolve_features(settings, cfg).enabled("sync.strava")
    if strava == settings.strava_enabled:
        return settings
    return settings.model_copy(update={"strava_enabled": strava})
