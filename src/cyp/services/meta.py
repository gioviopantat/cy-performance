"""App-shell metadata: athlete, season position, data version, last jobs."""

from __future__ import annotations

from typing import Any

from cyp import __version__
from cyp.dataset import data_version
from cyp.planning.season import build_skeleton
from cyp.schemas import AthleteSummary, Meta, SeasonSummary
from cyp.services.context import AppContext, NoDataError
from cyp.store.models import Athlete
from cyp.store.repo.job_runs import JobRunRepo


def meta(ctx: AppContext) -> Meta:
    """See :class:`~cyp.schemas.Meta`."""
    today = ctx.today()
    athlete = None
    with ctx.factory() as s:
        version = data_version(s)
        jobs: dict[str, dict[str, Any]] = {
            name: {
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }
            for name, run in JobRunRepo(s).latest_per_job().items()
        }
        try:
            ds = ctx.dataset()
        except NoDataError:
            ds = None
        if ds is not None:
            row = s.get(Athlete, ds.athlete_id)
            ftp = ds.ftp_on(today)
            athlete = AthleteSummary(
                athlete_id=ds.athlete_id,
                name=row.name if row else None,
                weight_kg=ds.weight_kg,
                ftp=ftp,
                w_kg=round(ftp / ds.weight_kg, 2) if ftp and ds.weight_kg else None,
                timezone=ctx.settings.cyp_timezone,
            )
    season = None
    cfg = ctx.athlete_config_or_none()
    if cfg is not None:
        sk = build_skeleton(cfg)
        week = sk.week_of(today)
        goal = next((g for g in cfg.goals if g.kind == "ftp_target"), None)
        season = SeasonSummary(
            start=sk.start,
            goal_date=sk.goal_date,
            goal_name=goal.name if goal else None,
            target_ftp=goal.target.get("ftp") if goal else None,
            week_index=week.index if week else None,
            weeks_total=len(sk.weeks),
            phase=week.phase if week else None,
        )
    return Meta(
        version=__version__,
        today=today,
        data_version=version,
        athlete=athlete,
        season=season,
        last_jobs=jobs,
        plan_mode=ctx.settings.cyp_plan_mode,
    )
