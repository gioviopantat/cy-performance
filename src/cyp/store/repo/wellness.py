"""``wellness_daily`` + ``fitness_daily`` (``*_icu`` columns) repository."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import FitnessDaily, WellnessDaily
from cyp.store.repo.base import Repo

_IMMUTABLE = frozenset({"athlete_id", "date_local"})


class WellnessRepo(Repo):
    """Upsert icu wellness rows and mirror CTL/ATL/TSB into ``fitness_daily``."""

    def get(self, athlete_id: int, date_local: dt.date) -> WellnessDaily | None:
        """Fetch one day."""
        return self.session.get(WellnessDaily, (athlete_id, date_local))

    def upsert(self, athlete_id: int, date_local: dt.date, values: dict[str, Any]) -> WellnessDaily:
        """Insert or update a wellness day; stamps ``fetched_at``."""
        row = self.get(athlete_id, date_local)
        if row is None:
            row = WellnessDaily(athlete_id=athlete_id, date_local=date_local)
            self.session.add(row)
        for key, val in values.items():
            if key in _IMMUTABLE:
                continue
            setattr(row, key, val)
        row.fetched_at = iso_utc(now_utc())
        self.flush()
        return row

    def mirror_fitness(
        self,
        athlete_id: int,
        date_local: dt.date,
        *,
        ctl: float | None,
        atl: float | None,
        tsb: float | None = None,
    ) -> FitnessDaily:
        """Copy icu CTL/ATL (and TSB = CTL - ATL unless given) into ``fitness_daily``.

        Only the ``*_icu`` columns are touched; the simulator owns ``*_sim``.
        """
        row = self.session.get(FitnessDaily, (athlete_id, date_local))
        if row is None:
            row = FitnessDaily(athlete_id=athlete_id, date_local=date_local)
            self.session.add(row)
        row.ctl_icu = ctl
        row.atl_icu = atl
        if tsb is None and ctl is not None and atl is not None:
            tsb = round(ctl - atl, 2)
        row.tsb_icu = tsb
        self.flush()
        return row

    def latest_date(self, athlete_id: int) -> dt.date | None:
        """Most recent wellness day stored for the athlete."""
        stmt = select(func.max(WellnessDaily.date_local)).where(
            WellnessDaily.athlete_id == athlete_id
        )
        value = self.session.scalar(stmt)
        if value is None:
            return None
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))

    def list_between(self, athlete_id: int, start: dt.date, end: dt.date) -> list[WellnessDaily]:
        """Rows with ``start <= date_local <= end``, ascending."""
        stmt = (
            select(WellnessDaily)
            .where(
                WellnessDaily.athlete_id == athlete_id,
                WellnessDaily.date_local >= start,
                WellnessDaily.date_local <= end,
            )
            .order_by(WellnessDaily.date_local)
        )
        return list(self.session.scalars(stmt))

    def fitness_between(self, athlete_id: int, start: dt.date, end: dt.date) -> list[FitnessDaily]:
        """``fitness_daily`` rows in the inclusive range, ascending."""
        stmt = (
            select(FitnessDaily)
            .where(
                FitnessDaily.athlete_id == athlete_id,
                FitnessDaily.date_local >= start,
                FitnessDaily.date_local <= end,
            )
            .order_by(FitnessDaily.date_local)
        )
        return list(self.session.scalars(stmt))
