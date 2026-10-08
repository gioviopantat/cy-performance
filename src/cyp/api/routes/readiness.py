"""Readiness verdicts."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Body, Depends

from cyp.api.deps import ctx, date_range
from cyp.schemas import ReadinessOut
from cyp.services import readiness as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/readiness", tags=["readiness"])
Ctx = Annotated[AppContext, Depends(ctx)]


@router.get("", response_model=list[ReadinessOut])
def history(c: Ctx, start: dt.date | None = None, end: dt.date | None = None) -> list[ReadinessOut]:
    """Stored verdicts (default: last 28 days)."""
    s, e = date_range(c, start, end, default_days=28)
    return service.history(c, s, e)


@router.get("/{day}", response_model=ReadinessOut)
def get_day(c: Ctx, day: dt.date) -> ReadinessOut:
    """Stored verdict, or computed on the fly when none is stored."""
    return service.get(c, day)


@router.post("/recompute", response_model=list[ReadinessOut])
def recompute(
    c: Ctx, dates: Annotated[list[dt.date] | None, Body(embed=True)] = None
) -> list[ReadinessOut]:
    """Score and store the given days (default: today)."""
    return service.recompute(c, dates or [c.today()])
