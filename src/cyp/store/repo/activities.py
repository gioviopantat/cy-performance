"""``activities`` repository: upsert by source id, lookups, pending queues."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cyp.core.activity import RIDE_SPORT_TYPES
from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import Activity, StreamFile
from cyp.store.repo.base import Repo

_IMMUTABLE = frozenset({"id"})


class ActivityRepo(Repo):
    """CRUD for unified activity rows. Domain conversion lives in the ingest layer (M1)."""

    def get(self, activity_id: int) -> Activity | None:
        """Fetch by internal id."""
        return self.session.get(Activity, activity_id)

    def get_by_strava_id(self, strava_id: int) -> Activity | None:
        """Fetch by Strava id."""
        return self.session.scalars(select(Activity).where(Activity.strava_id == strava_id)).first()

    def get_by_intervals_id(self, intervals_id: str) -> Activity | None:
        """Fetch by intervals.icu id."""
        return self.session.scalars(
            select(Activity).where(Activity.intervals_id == intervals_id)
        ).first()

    def upsert(self, values: dict[str, Any]) -> Activity:
        """Insert or update by ``strava_id`` / ``intervals_id`` (whichever is present).

        ``is_ride`` is derived from ``sport_type`` when not given; ``updated_at`` is stamped.
        """
        row: Activity | None = None
        if values.get("strava_id") is not None:
            row = self.get_by_strava_id(values["strava_id"])
        if row is None and values.get("intervals_id"):
            row = self.get_by_intervals_id(str(values["intervals_id"]))
        if row is None:
            row = Activity()
            self.session.add(row)
            row.fetched_at = iso_utc(now_utc())
        for key, val in values.items():
            if key in _IMMUTABLE:
                continue
            setattr(row, key, val)
        if "is_ride" not in values and row.sport_type:
            row.is_ride = row.sport_type in RIDE_SPORT_TYPES
        row.updated_at = iso_utc(now_utc())
        self.flush()
        return row

    def list_pending(self, stage: str, limit: int = 100) -> list[Activity]:
        """Activities whose ``pending_{stage}`` flag is set, oldest first.

        ``stage`` is one of ``detail``, ``streams``, ``analysis``.
        """
        col = getattr(Activity, f"pending_{stage}")
        stmt = select(Activity).where(col.is_(True)).order_by(Activity.start_utc).limit(limit)
        return list(self.session.scalars(stmt))

    def mark_done(self, activity: Activity, stage: str) -> None:
        """Clear ``pending_{stage}`` and set the matching ``stage_flags`` key."""
        setattr(activity, f"pending_{stage}", False)
        flags = dict(activity.stage_flags or {})
        flags[stage if stage != "analysis" else "analyzed"] = True
        activity.stage_flags = flags
        self.flush()

    def list_between(
        self, start_utc: str, end_utc: str, *, rides_only: bool = True
    ) -> list[Activity]:
        """Activities with ``start_utc`` in ``[start_utc, end_utc)``, ascending."""
        stmt = (
            select(Activity)
            .where(Activity.start_utc >= start_utc, Activity.start_utc < end_utc)
            .order_by(Activity.start_utc)
        )
        if rides_only:
            stmt = stmt.where(Activity.is_ride.is_(True))
        return list(self.session.scalars(stmt))

    def count(self) -> int:
        """Total number of activity rows."""
        return len(self.session.scalars(select(Activity.id)).all())

    def upsert_stream_file(self, activity_id: int, values: dict[str, Any]) -> StreamFile:
        """Insert or update the ``stream_files`` pointer row for ``activity_id``."""
        row = self.session.get(StreamFile, activity_id)
        if row is None:
            row = StreamFile(activity_id=activity_id)
            self.session.add(row)
        for key, val in values.items():
            if key == "activity_id":
                continue
            setattr(row, key, val)
        self.flush()
        return row

    def get_stream_file(self, activity_id: int) -> StreamFile | None:
        """Fetch the ``stream_files`` row, if the activity has streams stored."""
        return self.session.get(StreamFile, activity_id)
