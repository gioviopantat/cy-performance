"""Calendar view: planned vs done per day (docs/specs/calendar-view.md)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from cyp.api.deps import ctx
from cyp.schemas import CalendarOut
from cyp.services import calendar as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/calendar", tags=["calendar"])
Ctx = Annotated[AppContext, Depends(ctx)]


@router.get("", response_model=CalendarOut)
def get_calendar(c: Ctx, start: dt.date | None = None, end: dt.date | None = None) -> CalendarOut:
    """Whole weeks covering ``[start, end]`` (default: 4 weeks back, 2 ahead; max 63 days)."""
    today = c.today()
    s = start or today - dt.timedelta(weeks=4)
    e = end or today + dt.timedelta(weeks=2)
    if s > e:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    try:
        return service.calendar(c, s, e)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
