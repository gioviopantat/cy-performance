"""Daily / weekly reports."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from cyp.api.deps import ctx
from cyp.schemas import ReportOut
from cyp.services import reports as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/reports", tags=["reports"])
Ctx = Annotated[AppContext, Depends(ctx)]
Kind = Literal["daily", "weekly"]


@router.get("/{kind}", response_model=list[str])
def list_reports(c: Ctx, kind: Kind) -> list[str]:
    """Stored report stems, newest first (``2026-10-06`` / ``2026-W41``)."""
    return service.list_reports(c, kind)


@router.get("/{kind}/{stem}", response_model=ReportOut)
def get_report(c: Ctx, kind: Kind, stem: str) -> ReportOut:
    """Markdown + structured facts of one report."""
    found = service.get(c, kind, stem)
    if found is None:
        raise HTTPException(status_code=404, detail="no such report")
    return found


@router.post("/{kind}", response_model=ReportOut)
def build_report(c: Ctx, kind: Kind, day: dt.date | None = None) -> ReportOut:
    """Render (and store) the report for ``day`` (default today / this week)."""
    return service.build(c, kind, day or c.today())
