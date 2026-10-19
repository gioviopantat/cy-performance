"""``planned_workouts`` repository: the live (not cancelled / superseded) plan of a day."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, select

from cyp.store.models import IcuEvent, PlannedWorkout
from cyp.store.repo.base import Repo

#: Statuses of planned workouts that no longer stand (replaced or withdrawn).
DEAD_STATUSES: tuple[str, ...] = ("cancelled", "superseded")


def is_live() -> ColumnElement[bool]:
    """WHERE clause selecting live planned workouts."""
    return PlannedWorkout.status.not_in(DEAD_STATUSES)


class PlannedWorkoutRepo(Repo):
    """Read the live plan."""

    def executed_by(self, activity_id: int) -> PlannedWorkout | None:
        """The planned workout matched to ``activity_id``, if any."""
        return self.session.scalar(
            select(PlannedWorkout).where(PlannedWorkout.executed_activity_id == activity_id)
        )

    def first_live_on(self, day: dt.date) -> PlannedWorkout | None:
        """The first live slot of ``day``."""
        return self.session.scalar(
            select(PlannedWorkout)
            .where(PlannedWorkout.date_local == day, is_live())
            .order_by(PlannedWorkout.slot)
            .limit(1)
        )

    def moved_day(self, pw: PlannedWorkout) -> dt.date | None:
        """The day the athlete dragged ``pw``'s calendar event to in icu, else None."""
        if not pw.external_id:
            return None
        on = self.session.scalar(
            select(IcuEvent.start_date_local).where(IcuEvent.external_id == pw.external_id)
        )
        day = _date(on)
        return day if day is not None and day != pw.date_local else None

    def moved_onto(self, day: dt.date) -> list[PlannedWorkout]:
        """Live planned workouts of other days whose calendar event the athlete moved to ``day``."""
        ids = self.session.scalars(
            select(IcuEvent.external_id).where(
                IcuEvent.start_date_local >= day.isoformat(),
                IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
                IcuEvent.external_id.is_not(None),
            )
        ).all()
        if not ids:
            return []
        return list(
            self.session.scalars(
                select(PlannedWorkout)
                .where(
                    PlannedWorkout.external_id.in_(ids),
                    PlannedWorkout.date_local != day,
                    is_live(),
                )
                .order_by(PlannedWorkout.date_local, PlannedWorkout.slot)
            )
        )


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((value or "")[:10])
    except ValueError:
        return None
