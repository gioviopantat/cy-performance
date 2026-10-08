"""Engine and session factory built from :class:`cyp.settings.Settings`.

SQLite connections get ``journal_mode=WAL`` and ``foreign_keys=ON`` on connect; any other
dialect is used as-is (Postgres is a URL change, see ADR-0002).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from cyp.settings import Settings


def _sqlite_path(db_url: str) -> Path | None:
    """Filesystem path for a file-backed SQLite URL, else ``None``."""
    url = make_url(db_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


def make_engine(db_url: str, *, echo: bool = False) -> Engine:
    """Create an engine; for file SQLite ensure the parent directory exists and enable WAL."""
    path = _sqlite_path(db_url)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(db_url, echo=echo, future=True)
    if make_url(db_url).get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    return engine


def engine_from_settings(settings: Settings, *, echo: bool = False) -> Engine:
    """Engine for ``settings.cyp_db_url``."""
    return make_engine(settings.cyp_db_url, echo=echo)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine`` (expire_on_commit off for CLI use)."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error, always close."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping(engine: Engine) -> bool:
    """True if ``SELECT 1`` succeeds."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        return False
