"""FTP: status, what-if, accept."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends

from cyp.api.deps import ctx
from cyp.schemas import FtpAccept, FtpStatusOut, FtpWhatIf
from cyp.services import ftp as service
from cyp.services.context import AppContext

router = APIRouter(prefix="/ftp", tags=["ftp"])
Ctx = Annotated[AppContext, Depends(ctx)]


@router.get("", response_model=FtpStatusOut)
def get_ftp(c: Ctx, as_of: dt.date | None = None) -> FtpStatusOut:
    """Current FTP, MMP, CP fits vs icu, estimate series and the (never applied) proposal."""
    return service.status(c, as_of=as_of)


@router.post("/what-if", response_model=FtpStatusOut)
def what_if(c: Ctx, body: FtpWhatIf) -> FtpStatusOut:
    """Same as ``GET /ftp`` but evaluated against another FTP setting (nothing stored)."""
    return service.status(c, as_of=body.as_of, ftp=body.ftp)


@router.post("/accept", response_model=FtpStatusOut)
def accept(c: Ctx, body: FtpAccept) -> FtpStatusOut:
    """Record the athlete's FTP decision (manual settings row; icu is not changed)."""
    return service.accept(c, body.ftp, effective_from=body.effective_from, note=body.note)
