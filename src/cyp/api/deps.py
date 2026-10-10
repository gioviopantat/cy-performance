"""Request-scoped dependencies and helpers shared by the routers."""

from __future__ import annotations

import datetime as dt

from fastapi import HTTPException, Request

from cyp.api.registry import PROFILE_HEADER, ProfileRegistry
from cyp.services.context import AppContext
from cyp.services.jobs import JobManager


def ctx(request: Request) -> AppContext:
    """The selected profile's :class:`AppContext` (``X-CYP-Profile`` header, else default)."""
    registry: ProfileRegistry = request.app.state.profiles
    return registry.get(request.headers.get(PROFILE_HEADER))


def jobs(request: Request) -> JobManager:
    """The background job manager."""
    manager: JobManager = request.app.state.jobs
    return manager


def date_range(
    c: AppContext, start: dt.date | None, end: dt.date | None, *, default_days: int
) -> tuple[dt.date, dt.date]:
    """Resolve an optional ``[start, end]`` (default: the last ``default_days`` days)."""
    end = end or c.today()
    start = start or end - dt.timedelta(days=default_days - 1)
    if start > end:
        raise HTTPException(status_code=422, detail="start must be on or before end")
    if (end - start).days > 3 * 366:
        raise HTTPException(status_code=422, detail="range too long (max 3 years)")
    return start, end
