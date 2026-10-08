"""``activity_intervals`` repository (icu-detected intervals and our own detector)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select

from cyp.store.models import ActivityInterval
from cyp.store.repo.base import Repo


class ActivityIntervalRepo(Repo):
    """Replace-all semantics per ``(activity_id, source)``: icu may re-detect or edit intervals."""

    def replace(
        self, activity_id: int, source: str, rows: Iterable[dict[str, Any]]
    ) -> list[ActivityInterval]:
        """Delete existing rows for ``(activity_id, source)`` and insert ``rows``.

        Each row dict uses ``activity_intervals`` column names; ``idx`` is assigned from the
        iteration order when absent.
        """
        self.session.execute(
            delete(ActivityInterval).where(
                ActivityInterval.activity_id == activity_id, ActivityInterval.source == source
            )
        )
        out: list[ActivityInterval] = []
        for idx, values in enumerate(rows):
            row = ActivityInterval(
                activity_id=activity_id, source=source, idx=values.get("idx", idx)
            )
            for key, val in values.items():
                if key in {"id", "activity_id", "source", "idx"}:
                    continue
                setattr(row, key, val)
            self.session.add(row)
            out.append(row)
        self.flush()
        return out

    def list_for(self, activity_id: int, source: str | None = None) -> list[ActivityInterval]:
        """Intervals of an activity ordered by ``idx``."""
        stmt = (
            select(ActivityInterval)
            .where(ActivityInterval.activity_id == activity_id)
            .order_by(ActivityInterval.source, ActivityInterval.idx)
        )
        if source:
            stmt = stmt.where(ActivityInterval.source == source)
        return list(self.session.scalars(stmt))

    def count_for(self, activity_id: int, source: str = "icu") -> int:
        """Number of stored intervals for an activity/source."""
        return len(self.list_for(activity_id, source))
