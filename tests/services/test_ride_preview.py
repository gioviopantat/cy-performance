"""tomorrow_preview: dry-run planner vs the stored plan; never writes."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from cyp.schemas import PlanOut, PlanPreviewRequest
from cyp.services import ride_preview
from cyp.services.context import AppContext
from cyp.services.plan import preview
from cyp.services.ride_preview import tomorrow_preview
from cyp.store.models import PlannedWorkout, PlanRevision, PublishLog
from tests.api.conftest import TODAY

TOMORROW = TODAY + dt.timedelta(days=1)


def _counts(ctx: AppContext) -> tuple[int, int, int]:
    with ctx.factory() as s:
        return (
            s.scalar(select(func.count(PlannedWorkout.id))) or 0,
            s.scalar(select(func.count(PlanRevision.id))) or 0,
            s.scalar(select(func.count(PublishLog.id))) or 0,
        )


def _store(ctx: AppContext, template_id: str, params: dict[str, int], name: str) -> None:
    with ctx.factory() as s:
        s.add(
            PlannedWorkout(
                athlete_id=1,
                date_local=TOMORROW,
                external_id=f"cyp:test:{template_id}",
                template_id=template_id,
                name=name,
                intent="endurance",
                steps={"params": params},
                status="proposed",
            )
        )
        s.commit()


def test_no_stored_plan_is_a_change_and_nothing_is_written(ctx: AppContext) -> None:
    before = _counts(ctx)
    t = tomorrow_preview(ctx, TOMORROW)
    pick = next(d for d in preview(ctx).days if d.date == TOMORROW)
    assert _counts(ctx) == before  # dry run: no proposals, revisions or publishes
    if pick.template_id is None:
        assert t is None
        return
    assert t is not None and t.day == TOMORROW
    assert t.before_name is None and t.after_name == pick.name_zh
    assert t.changed is True


def test_same_workout_stored_is_unchanged(ctx: AppContext) -> None:
    pick = next(d for d in preview(ctx).days if d.date == TOMORROW)
    assert pick.template_id is not None  # the seeded week plans a session on Wednesday
    _store(ctx, pick.template_id, dict(pick.params), "stored")
    t = tomorrow_preview(ctx, TOMORROW)
    assert t is not None
    assert t.changed is False and t.before_name == "stored" and t.after_name == pick.name_zh
    assert t.reasons_zh == [] or all(isinstance(r, str) for r in t.reasons_zh)


def test_different_workout_stored_is_a_change_with_a_reason(ctx: AppContext) -> None:
    pick = next(d for d in preview(ctx).days if d.date == TOMORROW)
    other = "recovery_spin" if pick.template_id != "recovery_spin" else "z2_endurance_60"
    _store(ctx, other, {"total_min": 45} if other == "recovery_spin" else {}, "stored")
    t = tomorrow_preview(ctx, TOMORROW)
    assert t is not None and t.changed is True and t.before_name == "stored"
    assert t.reasons_zh  # the planner's day explanation says why


def test_day_outside_the_horizon(ctx: AppContext) -> None:
    assert tomorrow_preview(ctx, TODAY + dt.timedelta(days=400)) is None


def test_preview_reasons_never_say_yesterday(ctx: AppContext) -> None:
    import json
    from pathlib import Path

    forbidden = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "ride_report.tomorrow_forbidden.json"
        ).read_text("utf-8")
    )["phrases"]
    pick = next(d for d in preview(ctx).days if d.date == TOMORROW)
    other = "recovery_spin" if pick.template_id != "recovery_spin" else "z2_endurance_60"
    _store(ctx, other, {"total_min": 45} if other == "recovery_spin" else {}, "stored")
    t = tomorrow_preview(ctx, TOMORROW)
    assert t is not None
    for reason in t.reasons_zh:
        assert not any(p in reason for p in forbidden), reason


def test_preview_plans_as_tomorrow_morning(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner must see the ride just done as yesterday, like the 05:30 run will."""
    seen: list[dt.date | None] = []
    real = ride_preview.preview

    def spy(c: AppContext, req: PlanPreviewRequest | None = None) -> PlanOut:
        seen.append(req.today if req else None)
        return real(c, req)

    monkeypatch.setattr(ride_preview, "preview", spy)
    tomorrow_preview(ctx, TOMORROW)
    assert seen == [TOMORROW]
