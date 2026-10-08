"""``power_curve_snapshots`` repository."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from cyp.store.models import PowerCurveSnapshot
from cyp.store.repo.base import Repo

_KEY = frozenset({"athlete_id", "as_of_date", "window", "source"})


class PowerCurveRepo(Repo):
    """Upsert one snapshot per ``(athlete, as_of_date, window, source)``."""

    def get(
        self, athlete_id: int, as_of_date: dt.date, window: str, source: str = "icu"
    ) -> PowerCurveSnapshot | None:
        """Fetch a snapshot by its natural key."""
        stmt = select(PowerCurveSnapshot).where(
            PowerCurveSnapshot.athlete_id == athlete_id,
            PowerCurveSnapshot.as_of_date == as_of_date,
            PowerCurveSnapshot.window == window,
            PowerCurveSnapshot.source == source,
        )
        return self.session.scalars(stmt).first()

    def upsert(
        self,
        athlete_id: int,
        as_of_date: dt.date,
        window: str,
        values: dict[str, Any],
        *,
        source: str = "icu",
    ) -> PowerCurveSnapshot:
        """Insert or update a snapshot."""
        row = self.get(athlete_id, as_of_date, window, source)
        if row is None:
            row = PowerCurveSnapshot(
                athlete_id=athlete_id, as_of_date=as_of_date, window=window, source=source
            )
            self.session.add(row)
        for key, val in values.items():
            if key in _KEY or key == "id":
                continue
            setattr(row, key, val)
        self.flush()
        return row

    def latest(
        self, athlete_id: int, window: str, source: str = "icu"
    ) -> PowerCurveSnapshot | None:
        """Most recent snapshot for a window."""
        stmt = (
            select(PowerCurveSnapshot)
            .where(
                PowerCurveSnapshot.athlete_id == athlete_id,
                PowerCurveSnapshot.window == window,
                PowerCurveSnapshot.source == source,
            )
            .order_by(PowerCurveSnapshot.as_of_date.desc(), PowerCurveSnapshot.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    def list_for(self, athlete_id: int, window: str | None = None) -> list[PowerCurveSnapshot]:
        """All snapshots ascending by date (optionally one window)."""
        stmt = (
            select(PowerCurveSnapshot)
            .where(PowerCurveSnapshot.athlete_id == athlete_id)
            .order_by(PowerCurveSnapshot.as_of_date, PowerCurveSnapshot.window)
        )
        if window:
            stmt = stmt.where(PowerCurveSnapshot.window == window)
        return list(self.session.scalars(stmt))
