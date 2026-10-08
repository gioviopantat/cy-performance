"""Programmatic Alembic: upgrade to head and compare current vs head revisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(db_url: str, *, configure_logging: bool = False) -> Config:
    """Alembic ``Config`` pointing at the packaged ``migrations/`` directory."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("file_template", "%%(year)04d%%(month)02d%%(day)02d_%%(rev)s_%%(slug)s")
    cfg.attributes["url"] = db_url
    cfg.attributes["configure_logging"] = configure_logging
    return cfg


def upgrade_head(db_url: str) -> None:
    """``alembic upgrade head`` using our engine (creates the SQLite parent dir, sets pragmas)."""
    from cyp.store.db import make_engine

    engine = make_engine(db_url)
    try:
        with engine.connect() as connection:
            cfg = alembic_config(db_url)
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")
            connection.commit()
    finally:
        engine.dispose()


def head_revision(db_url: str) -> str | None:
    """Latest revision id present in the migration scripts."""
    script = ScriptDirectory.from_config(alembic_config(db_url))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    """Revision stamped in the database, or ``None`` for an unmigrated DB."""
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


@dataclass(frozen=True)
class SchemaStatus:
    """Outcome of :func:`schema_status`."""

    current: str | None
    head: str | None

    @property
    def up_to_date(self) -> bool:
        """True when the DB is stamped at the scripts' head."""
        return self.head is not None and self.current == self.head


def schema_status(engine: Engine, db_url: str) -> SchemaStatus:
    """Compare DB revision against the scripts' head."""
    return SchemaStatus(current=current_revision(engine), head=head_revision(db_url))
