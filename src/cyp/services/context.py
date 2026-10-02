"""Process-wide handles a service call needs: settings, engine, sessions, stores, config."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import ConfigError, CypError
from cyp.core.timeutil import now_utc
from cyp.dataset import CACHE, Dataset
from cyp.settings import DEFAULT_ATHLETE_CONFIG, AthleteConfig, Settings, load_athlete_config
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
            SchemaOutdatedError: migrations are pending.
        """
        self.check_schema()
        ds = CACHE.get(self.factory, power_fix_until=self.power_fix_until())
        if ds is None:
            raise NoDataError("no athlete in the store; run `cyp sync` (or `cyp dev seed`)")
        return ds

    def power_fix_until(self) -> dt.date | None:
        """``data_quality.power_zeros_excluded_until`` from athlete.yaml (``None`` if unset)."""
        cfg = self.athlete_config_or_none()
        return cfg.data_quality.power_zeros_excluded_until if cfg else None

    def close(self) -> None:
        """Dispose the engine."""
        self.engine.dispose()
