"""Process-wide handles a service call needs: settings, engine, sessions, stores, config."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.data_quality import DataQuality
from cyp.core.errors import ConfigError, CypError
from cyp.core.features import FeatureSet
from cyp.core.timeutil import now_utc
from cyp.dataset import CACHE, Dataset
from cyp.settings import (
    DEFAULT_ATHLETE_CONFIG,
    AthleteConfig,
    Settings,
    load_athlete_config,
    resolve_features,
    with_feature_switches,
)
from cyp.store.db import engine_from_settings, session_factory
from cyp.store.streams import StreamStore


class NoDataError(LookupError):
    """The store has no athlete yet (run a sync or seed demo data)."""


class SchemaOutdatedError(CypError):
    """The database is behind the code's migrations (run ``cyp db upgrade``)."""


@dataclass
class AppContext:
    """Built once per process (CLI invocation or API server)."""

    settings: Settings
    engine: Engine
    factory: sessionmaker[Session]
    store: StreamStore
    athlete_config_path: Path = DEFAULT_ATHLETE_CONFIG
    #: Serialises writes from concurrent API requests (SQLite has one writer anyway).
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    #: Frozen local wall-clock time (tests, demos); ``None`` = the real clock.
    fixed_now: dt.datetime | None = None
    _schema_ok: bool = False

    def check_schema(self) -> None:
        """Fail fast with a clear hint when migrations are pending (checked once per context).

        Raises:
            SchemaOutdatedError: the DB revision is not the code's head revision.
        """
        if self._schema_ok:
            return
        from cyp.store.migrate import schema_status

        st = schema_status(self.engine, self.settings.cyp_db_url)
        if not st.up_to_date:
            raise SchemaOutdatedError(
                f"database schema is {st.current or 'empty'}, code expects {st.head}: "
                "run `uv run cyp db upgrade` (then `uv run cyp analyze`)"
            )
        self._schema_ok = True

    @classmethod
    def from_settings(
        cls, settings: Settings, *, athlete_config: Path | str | None = None
    ) -> AppContext:
        """Engine + session factory + stream store for ``settings``.

        ``athlete_config`` defaults to the profile's (``settings.cyp_athlete_config``).
        """
        engine = engine_from_settings(settings)
        if athlete_config:  # one athlete.yaml for the plan, the flags and the Strava bridge
            settings = settings.model_copy(update={"cyp_athlete_config": Path(athlete_config)})
        settings = with_feature_switches(settings)
        return cls(
            settings,
            engine,
            session_factory(engine),
            StreamStore(settings.streams_dir),
            settings.cyp_athlete_config,
        )

    @property
    def reports_dir(self) -> Path:
        """``data/reports``."""
        return self.settings.reports_dir

    @property
    def tz(self) -> ZoneInfo:
        """Athlete time zone."""
        return ZoneInfo(self.settings.cyp_timezone)

    def now_local(self) -> dt.datetime:
        """Naive local wall-clock time."""
        if self.fixed_now is not None:
            return self.fixed_now
        return now_utc().astimezone(self.tz).replace(tzinfo=None)

    def today(self) -> dt.date:
        """Local calendar day."""
        return self.now_local().date()

    def raw_athlete_config(self) -> AthleteConfig:
        """``athlete.yaml`` exactly as written.

        Raises:
            ConfigError: missing or invalid.
        """
        return load_athlete_config(self.athlete_config_path)

    def athlete_config(self) -> AthleteConfig:
        """The config the planner uses: ``athlete.yaml`` + athlete rules (age, health, distance).

        Raises:
            ConfigError: missing or invalid.
        """
        return self.athlete_config_with_notes()[0]

    def athlete_config_with_notes(self) -> tuple[AthleteConfig, list[str]]:
        """:meth:`athlete_config` plus the zh-TW notes of the rules that changed it."""
        from cyp.planning.athlete_rules import effective

        raw = self.raw_athlete_config()
        flags = resolve_features(self.settings, raw)
        return effective(
            raw,
            self.today(),
            athlete_rules=flags.enabled("plan.athlete_rules"),
            long_ride_progression=flags.enabled("plan.long_ride_progression"),
            goal_menus=flags.enabled("plan.goal_menus"),
            variety=flags.enabled("plan.variety"),
        )

    def athlete_config_or_none(self) -> AthleteConfig | None:
        """Parsed ``athlete.yaml`` or ``None``."""
        try:
            return self.athlete_config()
        except ConfigError:
            return None

    def features(self) -> FeatureSet:
        """Resolved feature flags for this profile (ADR-0008); the only place to ask.

        Raises:
            ConfigError: unknown ids in ``CYP_FEATURES``.
        """
        try:
            raw = self.raw_athlete_config()
        except ConfigError:
            raw = None
        return resolve_features(self.settings, raw)

    def dataset(self) -> Dataset:
        """Cached snapshot.

        Raises:
            NoDataError: no athlete in the store.
            SchemaOutdatedError: migrations are pending.
        """
        self.check_schema()
        ds = CACHE.get(self.factory, data_quality=self.data_quality())
        if ds is None:
            raise NoDataError("no athlete in the store; run `cyp sync` (or `cyp dev seed`)")
        return ds

    def data_quality(self) -> DataQuality | None:
        """Resolved ``data_quality`` from athlete.yaml (``None`` without a config)."""
        cfg = self.athlete_config_or_none()
        return cfg.data_quality.resolved() if cfg else None

    def close(self) -> None:
        """Dispose the engine."""
        self.engine.dispose()
