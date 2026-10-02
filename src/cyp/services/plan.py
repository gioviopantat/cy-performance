"""Planning: preview (what-if, nothing stored), commit (store proposals), stored plan, season."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.analysis.longitudinal.run import load_latest_report
from cyp.dataset import Dataset
from cyp.planning.job import PlanOverrides, PlanRun, build_plan, plan_horizon
from cyp.planning.planner import ROLE_ZH, DayPlan
from cyp.planning.renderer import render
from cyp.planning.season import PHASE_ZH, build_skeleton, season_projection
from cyp.planning.templates import Repeat, ResolvedWorkout, load_library
from cyp.schemas import (
    FitnessPoint,
    PlannedDayOut,
    PlanOut,
    PlanPreviewRequest,
    PlanStep,
    SeasonOut,
    SeasonWeekOut,
    WeekTargetOut,
)
from cyp.services.context import AppContext


def _steps(w: ResolvedWorkout) -> list[PlanStep]:
    out: list[PlanStep] = []
    for it in w.items:
        if isinstance(it, Repeat):
            for st in it.steps:
                out.append(
                    PlanStep(
                        kind=st.kind,
                        duration_s=st.duration_s,
                        lo=st.lo,
                        hi=st.hi,
                        cadence=st.cadence,
                        cue=it.cue,
                        repeat=it.count,
                    )
                )
        else:
            out.append(
                PlanStep(
                    kind=it.kind,
                    duration_s=it.duration_s,
                    lo=it.lo,
                    hi=it.hi,
                    cadence=it.cadence,
                    cue=it.cue,
                )
            )
    return out


def _day(d: DayPlan, mutable_from: dt.date | None) -> PlannedDayOut:
    w = d.workout
    return PlannedDayOut(
        date=d.date,
        role=d.role,
        role_zh=ROLE_ZH[d.role],
        template_id=d.template_id,
        name_zh=w.name_zh if w else None,
        intent=d.intent,
        tss=round(d.tss, 1),
        minutes=d.minutes,
        max_minutes=d.max_minutes,
        outdoor=d.outdoor,
        params=dict(d.params),
        steps=_steps(w) if w else [],
        workout_text=render(w) if w else None,
        note_zh=w.note_zh if w else None,
        mutable=mutable_from is None or d.date >= mutable_from,
        explanation=d.explanation,
    )


def _projection(ds: Dataset, run: PlanRun) -> list[FitnessPoint]:
    if not run.days:
        return []
    first = run.days[0].date
    ctl, atl = ds.ctl_atl(first - dt.timedelta(days=1))
    traj = simulate(PMCState(first - dt.timedelta(days=1), ctl, atl), [d.tss for d in run.days])
    return [
        FitnessPoint(
            date=d.date,
            load=round(d.tss, 1),
            ctl=round(st.ctl, 2),
            atl=round(st.atl, 2),
            tsb=round(st.tsb, 2),
            source="sim",
            planned_load=round(d.tss, 1),
        )
        for d, st in zip(run.days, traj, strict=True)
    ]


def to_out(ds: Dataset, run: PlanRun, *, persisted: bool, ms: float | None = None) -> PlanOut:
    """Convert a :class:`PlanRun` to the API model."""
    weeks = []
    for start, t in sorted(run.week_targets.items()):
        in_week = [d for d in run.days if start <= d.date <= start + dt.timedelta(days=6)]
        w = t.week
        weeks.append(
            WeekTargetOut(
                week_start=start,
                index=w.index,
                phase=w.phase,
                phase_zh=PHASE_ZH[w.phase],
                recovery=w.recovery,
                test=w.test,
                target_tss=t.target_tss,
                target_hours=t.target_hours,
                ctl_start=round(t.ctl_start, 2),
                ctl_end=round(t.ctl_end, 2),
                planned_tss=round(sum(d.tss for d in in_week), 1) if in_week else None,
                planned_hours=round(sum(d.minutes for d in in_week) / 60, 2) if in_week else None,
                explanation=t.explanation,
            )
        )
    return PlanOut(
        today=run.today,
        season_key=run.season_key,
        persisted=persisted,
        days=[_day(d, run.mutable_from) for d in run.days],
        weeks=weeks,
        adaptations=run.adaptations,
        repairs=[r.explanation for r in run.repairs],
        violations=[
            {
                "rule": v.rule,
                "date": v.date.isoformat(),
                "detail_zh": v.detail_zh,
                "evidence": dict(v.evidence),
            }
            for v in run.violations
        ],
        needs_review=run.needs_review,
        projection=_projection(ds, run),
        changes=run.changes,
        compute_ms=round(ms, 2) if ms is not None else None,
    )


def _bias(ctx: AppContext) -> Mapping[str, float]:
    bias = (load_latest_report(ctx.reports_dir) or {}).get("planner_bias", {})
    return {str(k): float(v) for k, v in bias.items()} if isinstance(bias, dict) else {}


def _overrides(req: PlanPreviewRequest) -> PlanOverrides:
    return PlanOverrides(
        weekday_minutes={str(k): v for k, v in req.weekday_minutes.items()},
        weekly_max_minutes=req.weekly_max_minutes,
        date_minutes=dict(req.date_minutes),
        indoor_days=frozenset(req.indoor_days),
        readiness=req.readiness,
        bias=req.bias,
        horizon_days=req.horizon_days,
        ctl=req.ctl,
        atl=req.atl,
    )


def preview(ctx: AppContext, req: PlanPreviewRequest | None = None) -> PlanOut:
    """What-if plan (nothing stored): the UI calls this on every slider move."""
    req = req or PlanPreviewRequest()
    t0 = time.perf_counter()
    ds = ctx.dataset()
    today = req.today or ctx.today()
    now = ctx.now_local() if req.today is None else dt.datetime.combine(today, dt.time(6, 0))
    run = plan_horizon(
        ds,
        ctx.athlete_config(),
        today=today,
        now_local=now,
        bias=_bias(ctx),
        library=load_library(),
        overrides=_overrides(req),
    )
    return to_out(ds, run, persisted=False, ms=(time.perf_counter() - t0) * 1000)


def commit(ctx: AppContext, *, today: dt.date | None = None, trigger: str = "manual") -> PlanOut:
    """Plan and store the proposals (``planned_workouts`` + ``plan_revisions``)."""
    t0 = time.perf_counter()
    day = today or ctx.today()
    now = ctx.now_local() if today is None else dt.datetime.combine(day, dt.time(6, 0))
    with ctx.write_lock:
        run = build_plan(
            ctx.factory,
            ctx.athlete_config(),
            today=day,
            now_local=now,
            bias=_bias(ctx),
            persist=True,
            trigger=trigger,
        )
    return to_out(ctx.dataset(), run, persisted=True, ms=(time.perf_counter() - t0) * 1000)


def season(ctx: AppContext) -> SeasonOut:
    """Whole-season overview projected from the CTL going into the season (or today)."""
    cfg = ctx.athlete_config()
    ds = ctx.dataset()
    sk = build_skeleton(cfg)
    ctl, _ = ds.ctl_atl(min(ctx.today(), sk.start) - dt.timedelta(days=1))
    proj = season_projection(sk, ctl, weekly_max_minutes=cfg.availability.weekly_max_minutes)
    goal = next((g for g in cfg.goals if g.kind == "ftp_target"), None)
    checkpoints = {c.week: c.ftp for c in goal.checkpoints} if goal else {}
    return SeasonOut(
        start=sk.start,
        goal_date=sk.goal_date,
        goal_name=goal.name if goal else None,
        target_ftp=goal.target.get("ftp") if goal else None,
        ctl_start=round(ctl, 2),
        weeks=[
            SeasonWeekOut(
                index=w.index,
                start=w.start,
                phase=w.phase,
                phase_zh=PHASE_ZH[w.phase],
                recovery=w.recovery,
                test=w.test,
                hit_sessions=w.hit_sessions,
                target_tss=t.target_tss,
                target_hours=t.target_hours,
                ctl_end=t.ctl_end,
                checkpoint_ftp=checkpoints.get(w.index),
            )
            for w, t in zip(sk.weeks, proj, strict=True)
        ],
    )
