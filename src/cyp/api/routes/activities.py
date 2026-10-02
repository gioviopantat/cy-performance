"""Activities: list, detail, streams."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from cyp.api.deps import ctx
from cyp.schemas import ActivityDetail, ActivityPage, StreamSeries
from cyp.services import activities as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/activities", tags=["activities"])
Ctx = Annotated[AppContext, Depends(ctx)]


@router.get("", response_model=ActivityPage)
def list_activities(
    c: Ctx,
    start: dt.date | None = None,
    end: dt.date | None = None,
    sport: str | None = None,
    rides_only: bool = False,
    classification: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ActivityPage:
    """Newest first; filter by date range, sport, rides only, ride class."""
    return service.list_activities(
        c,
        start=start,
        end=end,
        sport=sport,
        rides_only=rides_only,
        classification=classification,
        offset=offset,
        limit=limit,
    )


@router.get("/{activity_id}", response_model=ActivityDetail)
def get_activity(c: Ctx, activity_id: int) -> ActivityDetail:
    """Stored per-ride analysis with its Explanation."""
    return service.get_activity(c, activity_id)


@router.get("/{activity_id}/streams", response_model=StreamSeries)
def get_streams(
    c: Ctx, activity_id: int, resolution_s: Annotated[int | None, Query(ge=1, le=600)] = None
) -> StreamSeries:
    """Chart-ready streams (bucket-averaged; ≤ 5 000 points by default)."""
    return service.streams(c, activity_id, resolution_s=resolution_s)
