"""Process-wide handles a service call needs: settings, engine, sessions, stores, config."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import ConfigError
from cyp.core.timeutil import now_utc
from cyp.dataset import CACHE, Dataset
from cyp.settings import DEFAULT_ATHLETE_CONFIG, AthleteConfig, Settings, load_athlete_config
from cyp.store.db import engine_from_settings, session_factory
from cyp.store.streams import StreamStore


class NoDataError(LookupError):
    """The store has no athlete yet (run a sync or seed demo data)."""


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

    @classmethod
    def from_settings(
        cls, settings: Settings, *, athlete_config: Path | str = DEFAULT_ATHLETE_CONFIG
    ) -> AppContext:
        """Engine + session factory + stream store for ``settings``."""
        engine = engine_from_settings(settings)
        return cls(
            settings,
            engine,
            session_factory(engine),
            StreamStore(settings.streams_dir),
            Path(athlete_config),
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

    def athlete_config(self) -> AthleteConfig:
        """Parsed ``athlete.yaml``.

        Raises:
            ConfigError: missing or invalid.
        """
        return load_athlete_config(self.athlete_config_path)

    def athlete_config_or_none(self) -> AthleteConfig | None:
        """Parsed ``athlete.yaml`` or ``None``."""
        try:
            return self.athlete_config()
        except ConfigError:
            return None

    def dataset(self) -> Dataset:
        """Cached snapshot.

        Raises:
            NoDataError: no athlete in the store.
        """
        ds = CACHE.get(self.factory)
        if ds is None:
            raise NoDataError("no athlete in the store; run `cyp sync` (or `cyp dev seed`)")
        return ds

    def close(self) -> None:
        """Dispose the engine."""
        self.engine.dispose()
