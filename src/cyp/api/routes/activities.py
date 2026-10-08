"""Activities: list, detail, streams."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from cyp.api.deps import ctx
from cyp.schemas import (
    ActivityDetail,
    ActivityPage,
    RideFeedbackIn,
    RideFeedbackOut,
    RideLogOut,
    RideLogSave,
    StravaDescriptionOut,
    StravaDescriptionRequest,
    StreamSeries,
)
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


@router.get("/{activity_id}/ride-log", response_model=RideLogOut)
def ride_log(c: Ctx, activity_id: int) -> RideLogOut:
    """WORKOUT + RIDE.LOG text for a Strava description (deterministic; no poem)."""
    from cyp.services.ride_log import build

    return _ride_log_out(build(c, activity_id))


def _ride_log_out(log: Any) -> RideLogOut:
    return RideLogOut(
        activity_id=log.activity_id,
        workout=log.workout,
        ride_log=log.ride_log,
        text=log.text,
        source="saved" if log.saved is not None else "generated",
        generated=log.generated,
    )


@router.post("/{activity_id}/ride-log", response_model=RideLogOut)
def save_ride_log(c: Ctx, activity_id: int, body: RideLogSave) -> RideLogOut:
    """Store the edited RIDE.LOG (e.g. with the acrostic poem); "" restores the generated one."""
    from cyp.services.ride_log import build, save

    build(c, activity_id)  # 404 for unknown / unanalysed rides before writing anything
    save(c, activity_id, body.text)
    return _ride_log_out(build(c, activity_id))


@router.post("/{activity_id}/strava-description", response_model=StravaDescriptionOut)
def strava_description(
    c: Ctx, activity_id: int, body: StravaDescriptionRequest
) -> StravaDescriptionOut:
    """Preview (``confirm=false``) or write the ride's RIDE.LOG to Strava (flag-gated)."""
    from fastapi import HTTPException

    from cyp.services.strava_write import StravaWriteBlockedError, push

    try:
        r = push(c, activity_id, body.text, write=body.confirm)
    except StravaWriteBlockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StravaDescriptionOut(
        strava_id=r.strava_id, before=r.before, after=r.after, written=r.written
    )


@router.get("/{activity_id}/feedback", response_model=RideFeedbackOut)
def get_feedback(c: Ctx, activity_id: int) -> RideFeedbackOut:
    """Post-ride RPE / feel: the web answer, else intervals.icu's."""
    from cyp.services.ride_feedback import get

    f = get(c, activity_id)
    return RideFeedbackOut(activity_id=f.activity_id, rpe=f.rpe, feel=f.feel, source=f.source)


@router.post("/{activity_id}/feedback", response_model=RideFeedbackOut)
def save_feedback(c: Ctx, activity_id: int, body: RideFeedbackIn) -> RideFeedbackOut:
    """Store post-ride RPE / feel; readiness is recomputed for the next day right away."""
    import datetime as dt

    from fastapi import HTTPException

    from cyp.core.errors import ConfigError
    from cyp.services import readiness as readiness_service
    from cyp.services.ride_feedback import save

    try:
        f = save(c, activity_id, body.rpe, body.feel)
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    ride = next((a for a in c.dataset().activities if a.id == activity_id), None)
    if ride is not None:
        readiness_service.recompute(c, [ride.date + dt.timedelta(days=1)])
    return RideFeedbackOut(activity_id=f.activity_id, rpe=f.rpe, feel=f.feel, source=f.source)
