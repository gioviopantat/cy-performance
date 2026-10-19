"""``activity_metrics`` repository: one row per analysed activity, replaced on recompute."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import Activity, ActivityMetrics
from cyp.store.repo.base import Repo

_IMMUTABLE = frozenset({"activity_id"})


class ActivityMetricsRepo(Repo):
    """Upsert / lookup for our per-ride analysis (docs/03 ``activity_metrics``)."""

    def get(self, activity_id: int) -> ActivityMetrics | None:
        """Fetch the metrics row for an activity, if analysed."""
        return self.session.get(ActivityMetrics, activity_id)

    def upsert(self, activity_id: int, values: dict[str, Any]) -> ActivityMetrics:
        """Insert or replace the row for ``activity_id``; stamps ``computed_at``."""
        row = self.get(activity_id)
        if row is None:
            row = ActivityMetrics(activity_id=activity_id)
            self.session.add(row)
        for key, val in values.items():
            if key in _IMMUTABLE:
                continue
            setattr(row, key, val)
        row.computed_at = iso_utc(now_utc())
        self.flush()
        return row

    def list_stale(self, algo_version: str, limit: int | None = None) -> list[Activity]:
        """Ride activities whose metrics are missing or computed by another ``algo_version``.

        Oldest first, only activities that have a stream file.
        """
        stmt = (
            select(Activity)
            .outerjoin(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
            .where(
                Activity.is_ride.is_(True),
                Activity.stream_file.has(),
                (ActivityMetrics.activity_id.is_(None))
                | (ActivityMetrics.algo_version != algo_version),
            )
            .order_by(Activity.start_utc)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def all_rows(self) -> list[ActivityMetrics]:
        """Every metrics row ordered by activity id."""
        return list(
            self.session.scalars(select(ActivityMetrics).order_by(ActivityMetrics.activity_id))
        )
