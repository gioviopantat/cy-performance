"""Alembic migrations apply to a fresh SQLite file and match the ORM metadata."""

from __future__ import annotations

from sqlalchemy import Engine, inspect, text

from cyp.store.migrate import schema_status
from cyp.store.models import Base


def test_upgrade_creates_every_table(migrated_engine: Engine, db_url: str) -> None:
    names = set(inspect(migrated_engine).get_table_names())
    expected = set(Base.metadata.tables)
    assert expected <= names, expected - names
    assert "alembic_version" in names
    status = schema_status(migrated_engine, db_url)
    assert status.up_to_date
    assert status.current == status.head


def test_explanation_columns_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    for table in ("activity_metrics", "readiness_daily", "planned_workouts", "plan_revisions"):
        cols = {c["name"] for c in insp.get_columns(table)}
        assert "explanation" in cols, table


def test_sqlite_wal_enabled(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert str(mode).lower() == "wal"
    assert fk == 1


def test_upgrade_is_idempotent(migrated_engine: Engine, db_url: str) -> None:
    from cyp.store.migrate import upgrade_head

    upgrade_head(db_url)
    assert schema_status(migrated_engine, db_url).up_to_date
