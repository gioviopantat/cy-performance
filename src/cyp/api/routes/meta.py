"""App shell: meta, fitness series, season, glossary, explain."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from cyp.api.deps import ctx, date_range
from cyp.schemas import ExplainOut, FitnessSeries, Meta, SeasonOut
from cyp.services import fitness as fitness_service
from cyp.services import meta as meta_service
from cyp.services import plan as plan_service
from cyp.services import trends as trends_service
from cyp.services.context import AppContext

router = APIRouter(tags=["meta"])
Ctx = Annotated[AppContext, Depends(ctx)]
GLOSSARY_DIR = Path(__file__).resolve().parents[4] / "docs" / "glossary"


@router.get("/meta", response_model=Meta)
def get_meta(c: Ctx) -> Meta:
    """Athlete, season position, data version and last job runs."""
    return meta_service.meta(c)


@router.get("/fitness", response_model=FitnessSeries)
def get_fitness(c: Ctx, start: dt.date | None = None, end: dt.date | None = None) -> FitnessSeries:
    """Daily load and CTL/ATL/TSB (default: last 120 days)."""
    s, e = date_range(c, start, end, default_days=120)
    return fitness_service.series(c, s, e)


@router.get("/season", response_model=SeasonOut)
def get_season(c: Ctx) -> SeasonOut:
    """Season skeleton with weekly targets projected from current fitness."""
    return plan_service.season(c)


@router.get("/trends/latest")
def get_trends(c: Ctx) -> dict[str, object]:
    """Last trends report (PMC agreement, CP fits, durability, TID, climbs, limiters)."""
    report = trends_service.latest(c)
    if report is None:
        raise HTTPException(status_code=404, detail="no trends report yet")
    return report


@router.post("/trends/recompute")
def recompute_trends(c: Ctx, as_of: dt.date | None = None) -> dict[str, object]:
    """Recompute and store the trends report (~100 ms)."""
    return trends_service.recompute(c, as_of=as_of)


@router.get("/explain/{key}", response_model=ExplainOut)
def get_explanation(c: Ctx, key: str) -> ExplainOut:
    """The persisted Explanation for ``key`` (e.g. ``ride:42``, ``readiness.2026-10-06``)."""
    found = trends_service.explain(c, key)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no explanation {key!r}")
    return found


@router.get("/glossary")
def list_glossary() -> list[str]:
    """Glossary term ids (``docs/glossary/*.md``)."""
    if not GLOSSARY_DIR.is_dir():
        return []
    return sorted(p.stem for p in GLOSSARY_DIR.glob("*.md") if p.stem != "README")


@router.get("/glossary/{term}", response_class=PlainTextResponse)
def get_glossary(term: str) -> str:
    """One glossary page as Markdown (zh-TW)."""
    if not term.replace("_", "").isalnum():
        raise HTTPException(status_code=404, detail="unknown term")
    path = GLOSSARY_DIR / f"{term}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="unknown term")
    return path.read_text(encoding="utf-8")
