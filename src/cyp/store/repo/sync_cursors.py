"""``sync_cursors`` repository: incremental sync positions per source/key."""

from __future__ import annotations

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import SyncCursor
from cyp.store.repo.base import Repo


class SyncCursorRepo(Repo):
    """Get/set cursor values such as ``strava/activities_after``."""

    def get(self, source: str, key: str) -> str | None:
        """Cursor value or ``None`` when unset."""
        row = self.session.get(SyncCursor, (source, key))
        return row.value if row else None

    def set(self, source: str, key: str, value: str) -> SyncCursor:
        """Upsert the cursor."""
        row = self.session.get(SyncCursor, (source, key))
        if row is None:
            row = SyncCursor(source=source, key=key)
            self.session.add(row)
        row.value = value
        row.updated_at = iso_utc(now_utc())
        self.flush()
        return row

    def delete(self, source: str, key: str) -> bool:
        """Remove a cursor; returns whether it existed."""
        row = self.session.get(SyncCursor, (source, key))
        if row is None:
            return False
        self.session.delete(row)
        self.flush()
        return True

    def all_for(self, source: str | None = None) -> list[SyncCursor]:
        """Every cursor, optionally filtered by source."""
        stmt = select(SyncCursor).order_by(SyncCursor.source, SyncCursor.key)
        if source:
            stmt = stmt.where(SyncCursor.source == source)
        return list(self.session.scalars(stmt))
