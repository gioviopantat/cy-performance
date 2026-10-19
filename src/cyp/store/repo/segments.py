"""``segments`` / ``segment_efforts`` repository (Strava official segments and PR history)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import Segment, SegmentEffort
from cyp.store.repo.base import Repo


class SegmentRepo(Repo):
    """Upsert segments (master data) and the efforts an activity recorded on them."""

    def get_segment(self, segment_id: int) -> Segment | None:
        """Fetch a segment by Strava id."""
        return self.session.get(Segment, segment_id)

    def upsert_segment(self, values: dict[str, Any]) -> Segment:
        """Insert or update a segment keyed by ``id`` (the Strava segment id)."""
        row = self.session.get(Segment, int(values["id"]))
        if row is None:
            row = Segment(id=int(values["id"]))
            self.session.add(row)
        for key, val in values.items():
            if key != "id":
                setattr(row, key, val)
        row.fetched_at = iso_utc(now_utc())
        self.flush()
        return row

    def get_effort(self, effort_id: int) -> SegmentEffort | None:
        """Fetch an effort by Strava effort id."""
        return self.session.get(SegmentEffort, effort_id)

    def upsert_effort(self, values: dict[str, Any]) -> SegmentEffort:
        """Insert or update an effort keyed by ``id`` (needs ``activity_id`` and ``segment_id``)."""
        row = self.session.get(SegmentEffort, int(values["id"]))
        if row is None:
            row = SegmentEffort(id=int(values["id"]))
            self.session.add(row)
        for key, val in values.items():
            if key != "id":
                setattr(row, key, val)
        row.fetched_at = iso_utc(now_utc())
        self.flush()
        return row

    def efforts_for_activity(self, activity_id: int) -> list[SegmentEffort]:
        """Efforts of one activity ordered by stream start index."""
        stmt = (
            select(SegmentEffort)
            .where(SegmentEffort.activity_id == activity_id)
            .order_by(SegmentEffort.start_index, SegmentEffort.id)
        )
        return list(self.session.scalars(stmt))

    def efforts_for_segment(self, segment_id: int) -> list[SegmentEffort]:
        """Every effort on a segment, fastest first (PR timeline input)."""
        stmt = (
            select(SegmentEffort)
            .where(SegmentEffort.segment_id == segment_id)
            .order_by(SegmentEffort.elapsed_s, SegmentEffort.start_utc)
        )
        return list(self.session.scalars(stmt))

    def count_segments(self) -> int:
        """Number of distinct segments stored."""
        return len(self.session.scalars(select(Segment.id)).all())
