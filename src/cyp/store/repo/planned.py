"""``planned_workouts`` repository: the live (not cancelled / superseded) plan of a day."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import ColumnElement, select

from cyp.store.models import PlannedWorkout
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
