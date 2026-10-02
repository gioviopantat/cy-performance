"""Planning: stored plan, what-if preview, commit."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from cyp.api.deps import ctx
from cyp.schemas import PlannedDayOut, PlanOut, PlanPreviewRequest
from cyp.services import plan as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/plan", tags=["plan"])
Ctx = Annotated[AppContext, Depends(ctx)]


@router.get("", response_model=list[PlannedDayOut])
def stored(
    c: Ctx, start: dt.date | None = None, days: Annotated[int, Query(ge=1, le=56)] = 14
) -> list[PlannedDayOut]:
    """Stored proposals (what the calendar will get), rendered with steps and workout text."""
    return service.stored(c, start, days)


@router.post("/preview", response_model=PlanOut)
def preview(c: Ctx, body: PlanPreviewRequest | None = None) -> PlanOut:
    """What-if plan (availability, days off, indoor days, readiness, bias, CTL); nothing stored."""
    return service.preview(c, body)


@router.post("/commit", response_model=PlanOut)
def commit(c: Ctx, today: dt.date | None = None) -> PlanOut:
    """Replan and store proposals (never writes to intervals.icu)."""
    return service.commit(c, today=today)
