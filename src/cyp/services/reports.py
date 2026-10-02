"""Daily / weekly report build and retrieval."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Literal

from cyp.reports.facts import daily_facts, week_bounds, weekly_facts
from cyp.reports.render import write
from cyp.schemas import ReportOut
from cyp.services.context import AppContext

Kind = Literal["daily", "weekly"]


def build(ctx: AppContext, kind: Kind, day: dt.date) -> ReportOut:
    """Render and store the report for ``day`` (weekly: the ISO week containing it)."""
    cfg = ctx.athlete_config_or_none()
    with ctx.factory() as s:
        if kind == "daily":
            facts = daily_facts(s, day, reports_dir=ctx.reports_dir, cfg=cfg)
        else:
            facts = weekly_facts(s, week_bounds(day)[0], reports_dir=ctx.reports_dir, cfg=cfg)
    path = write(kind, facts, ctx.reports_dir)
    return get(ctx, kind, path.stem)  # type: ignore[return-value]


def get(ctx: AppContext, kind: Kind, stem: str) -> ReportOut | None:
    """Stored report ``reports/{kind}/{stem}.md`` (+ facts)."""
    base = Path(ctx.reports_dir) / kind
    md, facts = base / f"{stem}.md", base / f"{stem}.json"
    if not md.is_file() or "/" in stem or ".." in stem:
        return None
    return ReportOut(
        kind=kind,
        stem=stem,
        markdown=md.read_text(encoding="utf-8"),
        facts=json.loads(facts.read_text(encoding="utf-8")) if facts.is_file() else {},
    )


def list_reports(ctx: AppContext, kind: Kind) -> list[str]:
    """Stems of stored reports, newest first."""
    base = Path(ctx.reports_dir) / kind
    return sorted((p.stem for p in base.glob("*.md")), reverse=True) if base.is_dir() else []
