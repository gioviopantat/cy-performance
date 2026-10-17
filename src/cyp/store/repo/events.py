"""``icu_events`` repository: raw mirror of the intervals.icu calendar."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import IcuEvent
from cyp.store.repo.base import Repo

#: Prefix of ``external_id`` on events we publish ourselves (docs/01 §5.6).
CYP_EXTERNAL_ID_PREFIX = "cyp:"


class IcuEventRepo(Repo):
    """Upsert / list / prune mirrored calendar events (keyed by icu event id)."""

    def get(self, event_id: int) -> IcuEvent | None:
        """Fetch by icu id."""
        return self.session.get(IcuEvent, event_id)

    def upsert(self, values: dict[str, Any]) -> IcuEvent:
        """Insert or update one event; ``values`` must contain ``id``."""
        event_id = int(values["id"])
        row = self.get(event_id)
        if row is None:
            row = IcuEvent(id=event_id)
            self.session.add(row)
        for key, val in values.items():
            if key == "id":
                continue
            setattr(row, key, val)
        row.fetched_at = iso_utc(now_utc())
        self.flush()
        return row

    def upsert_many(self, rows: Iterable[dict[str, Any]]) -> list[IcuEvent]:
        """Upsert a batch; returns the ORM rows in input order."""
        return [self.upsert(r) for r in rows]

    def list_between(
        self,
        start_local: str,
        end_local: str,
        *,
        category: str | None = None,
        ours: bool | None = None,
    ) -> list[IcuEvent]:
        """Events whose ``start_date_local`` falls in ``[start_local, end_local]``.

        ``start_local``/``end_local`` are ISO local date(-time) strings compared lexically, which
        is correct for ISO-8601. ``ours=True`` keeps only ``cyp:`` external ids, ``False``
        excludes them.
        """
        stmt = (
            select(IcuEvent)
            .where(IcuEvent.start_date_local >= start_local, IcuEvent.start_date_local <= end_local)
            .order_by(IcuEvent.start_date_local, IcuEvent.id)
        )
        if category:
            stmt = stmt.where(IcuEvent.category == category)
        if ours is True:
            stmt = stmt.where(IcuEvent.external_id.like(f"{CYP_EXTERNAL_ID_PREFIX}%"))
        elif ours is False:
            stmt = stmt.where(
                (IcuEvent.external_id.is_(None))
                | (IcuEvent.external_id.not_like(f"{CYP_EXTERNAL_ID_PREFIX}%"))
            )
        return list(self.session.scalars(stmt))

    def delete_missing(self, start_local: str, end_local: str, keep_ids: Iterable[int]) -> int:
        """Remove mirrored events in the window that icu no longer returned.

        Fitness-model events (``SET_EFTP``/``SET_FITNESS``/``FITNESS_DAYS``) are excluded from
        pruning because they come from a separate, un-windowed endpoint.
        """
        keep = set(keep_ids)
        stmt = select(IcuEvent.id).where(
            IcuEvent.start_date_local >= start_local,
            IcuEvent.start_date_local <= end_local,
            IcuEvent.category.not_in(FITNESS_MODEL_CATEGORIES),
        )
        stale = [i for i in self.session.scalars(stmt) if i not in keep]
        if stale:
            self.session.execute(delete(IcuEvent).where(IcuEvent.id.in_(stale)))
            self.flush()
        return len(stale)

    def count(self) -> int:
        """Number of mirrored events."""
        return len(self.session.scalars(select(IcuEvent.id)).all())


FITNESS_MODEL_CATEGORIES: tuple[str, ...] = ("SET_EFTP", "SET_FITNESS", "FITNESS_DAYS")
