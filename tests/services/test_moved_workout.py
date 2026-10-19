"""A workout the athlete dragged to another day in icu is judged where it went."""

from __future__ import annotations

import datetime as dt

from cyp.services.context import AppContext
from cyp.store.models import Athlete, IcuEvent, PlannedWorkout
from cyp.store.repo.planned import PlannedWorkoutRepo

TUE, MON = dt.date(2026, 10, 20), dt.date(2026, 10, 19)
EXT = "cyp:s20261005:2026-10-20:1"


def test_a_dragged_workout_belongs_to_the_day_it_sits_on(ctx: AppContext) -> None:
    with ctx.factory() as s:
        athlete = s.query(Athlete).first()
        assert athlete is not None
        s.query(PlannedWorkout).filter(PlannedWorkout.date_local.in_([MON, TUE])).delete()
        s.add(
            PlannedWorkout(
                athlete_id=athlete.id, date_local=TUE, slot=1, external_id=EXT,
                template_id="ss_3x_n", template_version=1, intent="sweetspot",
                name="甜蜜點 3x16 分", target_tss=79.0, status="proposed",
            )
        )  # fmt: skip
        s.add(IcuEvent(id=987654, category="WORKOUT", external_id=EXT,
                       start_date_local=f"{MON.isoformat()}T00:00:00"))  # fmt: skip
        s.commit()
        repo = PlannedWorkoutRepo(s)
        pw = repo.first_live_on(TUE)
        assert pw is not None and repo.moved_day(pw) == MON
        assert [p.external_id for p in repo.moved_onto(MON)] == [EXT]
        assert repo.moved_onto(TUE) == []
        # Back where we put it: nothing moved.
        ev = s.get(IcuEvent, 987654)
        assert ev is not None
        ev.start_date_local = f"{TUE.isoformat()}T00:00:00"
        s.commit()
        assert repo.moved_day(pw) is None and repo.moved_onto(MON) == []
