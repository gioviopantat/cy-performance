"""``activity_zones`` repository (Strava per-activity HR/power distribution buckets)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import ActivityZones
from cyp.store.repo.base import Repo


class ActivityZonesRepo(Repo):
    """One row per ``(activity_id, zone_type)``."""

    def get(self, activity_id: int, zone_type: str) -> ActivityZones | None:
        """Fetch the distribution of one type for an activity."""
        stmt = select(ActivityZones).where(
            ActivityZones.activity_id == activity_id, ActivityZones.zone_type == zone_type
        )
        return self.session.scalars(stmt).first()

    def upsert(self, activity_id: int, zone_type: str, values: dict[str, Any]) -> ActivityZones:
        """Insert or update the ``(activity_id, zone_type)`` row."""
        row = self.get(activity_id, zone_type)
        if row is None:
            row = ActivityZones(activity_id=activity_id, zone_type=zone_type)
            self.session.add(row)
        for key, val in values.items():
            if key not in {"id", "activity_id", "zone_type"}:
                setattr(row, key, val)
        row.fetched_at = iso_utc(now_utc())
        self.flush()
        return row

    def for_activity(self, activity_id: int) -> list[ActivityZones]:
        """All zone distributions stored for an activity."""
        stmt = (
            select(ActivityZones)
            .where(ActivityZones.activity_id == activity_id)
            .order_by(ActivityZones.zone_type)
        )
        return list(self.session.scalars(stmt))
