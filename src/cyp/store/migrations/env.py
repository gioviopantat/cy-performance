"""Alembic environment.

The URL comes from ``config.attributes['url']`` (set by ``cyp.store.migrate``) or falls back
to ``sqlalchemy.url`` in alembic.ini / ``CYP_DB_URL``.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from cyp.store.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    if not url:
        url = os.environ.get("CYP_DB_URL", "sqlite:///data/cyp.sqlite")
    return str(url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection (reusing one passed via attributes if any)."""
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _run_with_connection(connectable)
        return
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run_with_connection(connection)


def _run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite ALTER TABLE support
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
