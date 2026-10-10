"""Rolling-horizon planning: pure :func:`plan_horizon` + :func:`build_plan` job (docs/05 §3).

:func:`plan_horizon` (pure, over a :class:`~cyp.dataset.Dataset`) does:

1. Season skeleton from the athlete config.
2. Horizon ``today .. today + horizon_days - 1`` clipped to the season.
3. Week targets from the CTL going into each week (store for the current week, simulation of
   the planned week for the next).
4. :func:`~cyp.planning.planner.plan_week` per week with limiter bias, availability overrides
   (icu NOTE ``training_availability=LIMITED`` + ``max_training_time`` in seconds;
   HOLIDAY/SICK/INJURED -> 0) and any :class:`PlanOverrides` (UI what-ifs).
5. Adapt today (calendar, yesterday, readiness) and enforce guardrails on mutable days.

:func:`build_plan` = cached dataset -> :func:`plan_horizon` -> (optionally) persist
``seasons`` / ``blocks`` / ``week_plans`` / ``planned_workouts`` (``proposed``; superseding
older proposals for mutable dates) and one ``plan_revisions`` row.

Nothing here talks to intervals.icu; publishing is :mod:`cyp.publish` and needs explicit flags.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.core.errors import AnalysisError
from cyp.core.explain import Explanation
from cyp.core.ids import external_id
from cyp.core.timeutil import iso_utc, now_utc
from cyp.dataset import CACHE, Dataset, PlannedRow
from cyp.planning import adapt
from cyp.planning.athlete_rules import is_senior
from cyp.planning.guardrails import RULE_ZH, GuardrailInputs, Repair, Violation, enforce
from cyp.planning.planner import DayPlan, PlanContext, Role, explain_day, plan_week
from cyp.planning.season import SeasonSkeleton, SeasonWeek, build_skeleton, week_targets
from cyp.planning.templates import Template, TemplateError, load_library, resolve
from cyp.settings import AthleteConfig
from cyp.store.models import Block as BlockRow
from cyp.store.models import PlannedWorkout, PlanRevision, WeekPlan
from cyp.store.models import Season as SeasonRow

SEASON_KEY_FMT = "s{start:%Y%m%d}"
MUTABLE_STATUSES = ("proposed", "published")
HARD_CLASSES = ("sweetspot", "threshold", "vo2", "race")


def season_key(sk: SeasonSkeleton) -> str:
    """Stable season key used inside ``external_id``."""
    return SEASON_KEY_FMT.format(start=sk.start)


@dataclass(frozen=True)
class PlanOverrides:
    """What-if inputs (never persisted unless the caller persists the resulting plan).

    ``weekday_minutes`` replaces per-weekday availability (``{"tue": 60}``), ``date_minutes``
    caps single dates (0 = day off), ``indoor_days`` forces indoor renderings, ``readiness``
    forces today's recommendation, ``bias`` replaces the limiter bias, ``ctl`` / ``atl``
    replace the fitness going into the horizon.
    """

    weekday_minutes: Mapping[str, int] = field(default_factory=dict)
    weekly_max_minutes: int | None = None
    date_minutes: Mapping[dt.date, int] = field(default_factory=dict)
    indoor_days: frozenset[dt.date] = frozenset()
    readiness: str | None = None
    bias: Mapping[str, float] | None = None
    horizon_days: int | None = None
    ctl: float | None = None
    atl: float | None = None

    def apply_to(self, cfg: AthleteConfig) -> AthleteConfig:
        """Config with the availability / horizon overrides applied."""
        avail: dict[str, Any] = dict(self.weekday_minutes)
        if self.weekly_max_minutes is not None:
            avail["weekly_max_minutes"] = self.weekly_max_minutes
        update: dict[str, Any] = {}
        if avail:
            update["availability"] = cfg.availability.model_copy(update=avail)
        if self.horizon_days is not None:
            update["planner"] = cfg.planner.model_copy(update={"horizon_days": self.horizon_days})
        return cfg.model_copy(update=update) if update else cfg


@dataclass
class PlanRun:
    """Result of one planning run."""

    today: dt.date
    season_key: str
    days: list[DayPlan] = field(default_factory=list)
    week_targets: dict[dt.date, Any] = field(default_factory=dict)
    adaptations: list[Explanation] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    changes: dict[str, list[str]] = field(default_factory=dict)
    revision_id: int | None = None
    mutable_from: dt.date | None = None

    @property
    def needs_review(self) -> bool:
        """Guardrails could not be satisfied."""
        return bool(self.violations)


def _overrides_and_events(
    ds: Dataset, start: dt.date, end: dt.date
) -> tuple[dict[dt.date, int], dict[dt.date, list[str]]]:
    overrides: dict[dt.date, int] = {}
    athlete: dict[dt.date, list[str]] = {}
    for e in ds.events_between(start, end):
        if e.ours or e.date is None:
            continue
        if e.category == "NOTE" and (e.training_availability or "").upper() == "LIMITED":
            minutes = (e.max_training_time or 0) // 60
            overrides[e.date] = min(overrides.get(e.date, minutes), minutes)
        elif e.category in adapt.BLOCKING_CATEGORIES:
            overrides[e.date] = 0
            athlete.setdefault(e.date, []).append(e.category)
        elif e.category in adapt.ATHLETE_OWNS:
            athlete.setdefault(e.date, []).append(e.category)
    return overrides, athlete


def _longest_ride_min(ds: Dataset, today: dt.date) -> float | None:
    minutes = [
        a.moving_s / 60
        for a in ds.activities
        if a.is_ride and a.moving_s and today - dt.timedelta(weeks=6) <= a.date < today
    ]
    return max(minutes) if minutes else None


def _dayplan_from_row(row: PlannedRow, library: Mapping[str, Template]) -> DayPlan | None:
    t = library.get(row.template_id or "")
    if t is None:
        return None
    try:
        w = resolve(t, dict(row.params), outdoor=not row.indoor)
    except TemplateError:
        return None
    return DayPlan(
        date=row.date,
        role=cast(Role, row.role),
        max_minutes=round(w.duration_s / 60),
        template_id=t.id,
        params=dict(w.params),
        outdoor=not row.indoor,
        workout=w,
        intent=t.intent,
    )


def _calendar_showed(ds: Dataset, row: PlannedRow, plan: DayPlan) -> bool:
    """Whether the calendar event of ``row`` (if synced) shows the workout of ``plan``."""
    ev = next((e for e in ds.events if e.external_id == row.external_id), None)
    if ev is None or ev.name is None or plan.workout is None:
        return True
    return ev.name.strip() == plan.workout.name_zh.strip()


def _progress(week: SeasonWeek) -> tuple[str, float, float]:
    n_load = 6 if week.phase in ("base", "build", "threshold") else 2
    step = 1 / max(n_load - 1, 1)
    return week.phase, (week.progression_index - 1) * step, step


def plan_horizon(
    ds: Dataset,
    cfg: AthleteConfig,
    *,
    today: dt.date,
    now_local: dt.datetime,
    bias: Mapping[str, float] | None = None,
    library: Mapping[str, Template] | None = None,
    overrides: PlanOverrides | None = None,
) -> PlanRun:
    """Plan the horizon starting ``today`` (pure; see module docstring)."""
    ov = overrides or PlanOverrides()
    cfg = ov.apply_to(cfg)
    lib = library if library is not None else load_library()
    sk = build_skeleton(cfg)
    run = PlanRun(today=today, season_key=season_key(sk))
    first = max(today, sk.start)
    last = min(today + dt.timedelta(days=cfg.planner.horizon_days - 1), sk.weeks[-1].end)
    if first > last:
        return run
    cutoff = cfg.planner.today_cutoff_local
    mutable_from = today if now_local.time() < cutoff else today + dt.timedelta(days=1)
    run.mutable_from = mutable_from

    cal_minutes, athlete_events = _overrides_and_events(ds, first, last)
    for d, m in ov.date_minutes.items():
        cal_minutes[d] = min(cal_minutes.get(d, m), m)
    ctx = PlanContext(
        cfg=cfg,
        library=lib,
        bias=dict(ov.bias if ov.bias is not None else (bias or {})),
        overrides=cal_minutes,
        indoor_days=frozenset(ov.indoor_days),
        goal_date=sk.goal_date,
        masters=is_senior(cfg, today),
    )
    weeks = [w for w in sk.weeks if w.end >= first and w.start <= last]
    ctl, atl = ds.ctl_atl(weeks[0].start - dt.timedelta(days=1))
    if ov.ctl is not None:
        ctl = ov.ctl
        atl = ov.atl if ov.atl is not None else ctl
    prev_loading: float | None = None
    all_days: list[DayPlan] = []
    for w in weeks:
        t = week_targets(
            w,
            ctl,
            weekly_max_minutes=cfg.availability.weekly_max_minutes,
            prev_loading_tss=prev_loading,
            atl_start=atl,
        )
        run.week_targets[w.start] = t
        days = plan_week(w, t, ctx)
        all_days += days
        seed = PMCState(w.start - dt.timedelta(days=1), ctl, atl)
        end = simulate(
            seed, [ds.loads.get(d.date, 0.0) if d.date < today else d.tss for d in days]
        )[-1]
        ctl, atl = end.ctl, end.atl
        if not w.recovery and w.phase != "test":
            prev_loading = t.target_tss
    horizon = [d for d in all_days if first <= d.date <= last]

    run.adaptations += adapt.apply_calendar(horizon, athlete_events)
    week_today = sk.week_of(today)
    if week_today is not None:
        phase, progress, step = _progress(week_today)
        y = today - dt.timedelta(days=1)
        hard_y = any(a.classification in HARD_CLASSES for a in ds.on(y) if a.is_ride)
        planned_rows = ds.planned.get(y, ())
        planned_y = _dayplan_from_row(planned_rows[0], lib) if planned_rows else None
        if planned_y is not None and not _calendar_showed(ds, planned_rows[0], planned_y):
            # A later proposal for a frozen day: the athlete saw another workout, so judging
            # yesterday against our proposal is wrong (readiness compares with the calendar).
            planned_y = None
        run.adaptations += adapt.apply_yesterday(
            horizon,
            adapt.Yesterday(planned_y, ds.loads.get(y), hard_y),
            today,
            ctx,
            phase,
            progress,
        )
        r = ds.readiness.get(today)
        rec = ov.readiness or (r.recommendation if r else None)
        fit_y = ds.fitness.get(y)
        if mutable_from == today:
            run.adaptations += adapt.apply_readiness(
                horizon,
                today,
                rec,
                tsb=fit_y.tsb if fit_y else None,
                hard_yesterday=hard_y,
                ctx=ctx,
                phase=phase,
                progress=progress,
                progress_step=step,
            )

    seed_day = first - dt.timedelta(days=1)
    c0, a0 = ds.ctl_atl(seed_day)
    if ov.ctl is not None:
        c0, a0 = ov.ctl, ov.atl if ov.atl is not None else ov.ctl
    low = frozenset(
        d
        for d, r in ds.readiness.items()
        if d >= first - dt.timedelta(days=7) and (r.score or 100) < 40
    )
    gi = GuardrailInputs(
        seed=PMCState(seed_day, c0, a0),
        phase_of=lambda d: (sk.week_of(d) or weeks[0]).phase,
        recovery_weeks=frozenset(w.start for w in sk.weeks if w.recovery or w.phase == "test"),
        history_loads={d: v for d, v in ds.loads.items() if d < first},
        longest_ride_min_6w=_longest_ride_min(ds, today),
        low_readiness_days=low,
        ramp_cap=cfg.planner.ramp_cap,
        tsb_floor=cfg.planner.tsb_floor,
        hit_per_week=cfg.planner.hit_per_week,
    )
    run.repairs, run.violations = enforce(horizon, gi, ctx, mutable_from=mutable_from)
    _refresh_explanations(horizon, run, sk, ctx)
    run.days = horizon
    return run


def _refresh_explanations(
    days: list[DayPlan], run: PlanRun, sk: SeasonSkeleton, ctx: PlanContext
) -> None:
    """Re-explain every day after adaptations and guardrail repairs.

    ``plan_week`` explains the workout it chose; a later swap (yesterday over plan, readiness,
    calendar, guardrails) would otherwise publish the old workout's reasons. Swap reasons are
    kept as day notes, so they appear in the new explanation.
    """

    def name(tid: str | None) -> str:
        """Template name without unresolved ``{param}`` parts (e.g. "恢復騎 {total_min} 分")."""
        t = ctx.library.get(tid or "")
        if t is None:
            return "休息"
        return t.name_zh.split("{")[0].strip() or t.name_zh

    by_day: dict[dt.date, list[Repair]] = {}
    for r in run.repairs:
        by_day.setdefault(r.date, []).append(r)
    for date, reps in by_day.items():
        day = next((d for d in days if d.date == date), None)
        if day is None:
            continue
        # One plain-language line per day: first "before" -> final "after", last rule's numbers.
        last = reps[-1]
        rule = RULE_ZH.get(last.violation.rule, last.violation.rule)
        note = (
            f"為了守住「{rule}」：{name(reps[0].before)} → "
            f"{day.workout.name_zh if day.workout is not None else '休息'}"
            f"（{last.violation.detail_zh}）"
        )
        if note not in day.notes:
            day.notes.append(note)
    for day in days:
        week = sk.week_of(day.date)
        targets = run.week_targets.get(week.start) if week is not None else None
        if week is not None and targets is not None:
            day.explanation = explain_day(day, week, targets, ctx)


def build_plan(
    factory: sessionmaker[Session],
    cfg: AthleteConfig,
    *,
    today: dt.date,
    now_local: dt.datetime,
    bias: Mapping[str, float] | None = None,
    library: Mapping[str, Template] | None = None,
    persist: bool = True,
    trigger: str = "manual",
    overrides: PlanOverrides | None = None,
) -> PlanRun:
    """Plan from the cached dataset and (optionally) persist.

    Raises:
        AnalysisError: no athlete in the store.
    """
    ds = CACHE.get(factory, data_quality=cfg.data_quality.resolved())
    if ds is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    run = plan_horizon(
        ds, cfg, today=today, now_local=now_local, bias=bias, library=library, overrides=overrides
    )
    if persist and run.days:
        with factory() as s:
            _persist(
                s,
                run,
                build_skeleton((overrides or PlanOverrides()).apply_to(cfg)),
                ds.athlete_id,
                cfg,
                run.mutable_from or today,
                trigger,
            )
            s.commit()
    return run


# --------------------------------------------------------------------------- persistence


def _season_row(s: Session, sk: SeasonSkeleton, athlete_id: int, cfg: AthleteConfig) -> SeasonRow:
    name = f"ftp_target {sk.start.isoformat()}"
    row = s.scalars(
        select(SeasonRow).where(SeasonRow.athlete_id == athlete_id, SeasonRow.name == name)
    ).first()
    if row is None:
        row = SeasonRow(
            athlete_id=athlete_id,
            name=name,
            start_date=sk.start,
            end_date=sk.weeks[-1].end,
            created_at=iso_utc(now_utc()),
        )
        s.add(row)
    row.config = {
        "type": cfg.season.type,
        "load_pattern": cfg.season.load_pattern,
        "goal_date": sk.goal_date.isoformat(),
        "key": season_key(sk),
    }
    s.flush()
    for b_idx in sorted({w.block_idx for w in sk.weeks}):
        bw = sk.block_weeks(b_idx)
        brow = s.scalars(
            select(BlockRow).where(BlockRow.season_id == row.id, BlockRow.idx == b_idx)
        ).first()
        if brow is None:
            brow = BlockRow(
                season_id=row.id,
                idx=b_idx,
                phase=bw[0].phase,
                start_date=bw[0].start,
                end_date=bw[-1].end,
                weeks=len(bw),
            )
            s.add(brow)
        brow.load_pattern = cfg.season.load_pattern
        brow.focus = {"phase": bw[0].phase}
    s.flush()
    return row


def _week_row(s: Session, season: SeasonRow, w: SeasonWeek, targets: Any) -> WeekPlan:
    brow = s.scalars(
        select(BlockRow).where(BlockRow.season_id == season.id, BlockRow.idx == w.block_idx)
    ).one()
    row = s.scalars(
        select(WeekPlan).where(WeekPlan.block_id == brow.id, WeekPlan.week_start == w.start)
    ).first()
    if row is None:
        row = WeekPlan(block_id=brow.id, week_start=w.start)
        s.add(row)
    row.target_tss = targets.target_tss
    row.target_hours = targets.target_hours
    row.hit_sessions = w.hit_sessions
    row.planned_ctl_end = targets.ctl_end
    row.notes = targets.explanation.headline_zh
    s.flush()
    return row


def _persist(
    s: Session,
    run: PlanRun,
    sk: SeasonSkeleton,
    athlete_id: int,
    cfg: AthleteConfig,
    mutable_from: dt.date,
    trigger: str,
) -> None:
    season = _season_row(s, sk, athlete_id, cfg)
    week_rows = {
        w.start: _week_row(s, season, w, run.week_targets[w.start])
        for w in sk.weeks
        if w.start in run.week_targets
    }
    exts = [external_id(run.season_key, d.date, 1) for d in run.days]
    existing_rows = {
        r.external_id: r
        for r in s.scalars(select(PlannedWorkout).where(PlannedWorkout.external_id.in_(exts)))
    }
    before: dict[str, dict[str, Any]] = {}
    after: dict[str, dict[str, Any]] = {}
    created: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    for d in run.days:
        if d.date < mutable_from:
            continue
        ext = external_id(run.season_key, d.date, 1)
        existing = existing_rows.get(ext)
        if existing is not None:
            before[ext] = {
                "template_id": existing.template_id,
                "params": (existing.steps or {}).get("params"),
                "status": existing.status,
            }
        if d.workout is None:
            if existing is not None and existing.status in MUTABLE_STATUSES:
                existing.status = "superseded"
                removed.append(ext)
            continue
        snapshot = {"template_id": d.template_id, "params": dict(d.params), "status": "proposed"}
        after[ext] = snapshot
        week = sk.week_of(d.date)
        values = dict(
            week_plan_id=week_rows[week.start].id if week and week.start in week_rows else None,
            athlete_id=athlete_id,
            date_local=d.date,
            slot=1,
            external_id=ext,
            template_id=d.template_id,
            template_version=str(d.workout.template_version),
            name=d.workout.name_zh,
            intent=d.intent,
            steps={"params": dict(d.params), "role": d.role, "outdoor": d.outdoor},
            target_tss=round(d.tss, 1),
            target_duration_s=d.workout.duration_s,
            target_if=round(d.workout.if_, 3),
            indoor=not d.outdoor,
            target_mode="POWER",
            explanation=d.explanation.to_json_dict() if d.explanation else None,
        )
        if existing is None:
            s.add(PlannedWorkout(status="proposed", created_by="planner", revision=1, **values))
            created.append(ext)
        elif existing.status in MUTABLE_STATUSES:
            same = (
                existing.template_id == d.template_id
                and (existing.steps or {}).get("params") == dict(d.params)
                and bool(existing.indoor) == (not d.outdoor)
            )
            for k, v in values.items():
                setattr(existing, k, v)
            if not same:
                existing.revision = (existing.revision or 1) + 1
                existing.status = "proposed"
                changed.append(ext)
    # Proposals in this horizon that the run did not produce (e.g. an older season key after
    # season.start moved) would otherwise linger next to the new ones: supersede them.
    if run.days:
        stale = s.scalars(
            select(PlannedWorkout).where(
                PlannedWorkout.athlete_id == athlete_id,
                PlannedWorkout.date_local >= mutable_from,
                PlannedWorkout.date_local <= run.days[-1].date,
                PlannedWorkout.external_id.not_in(exts),
                PlannedWorkout.status.in_(MUTABLE_STATUSES),
            )
        )
        for row in stale:
            row.status = "superseded"
            removed.append(row.external_id)
    run.changes = {"created": created, "changed": changed, "removed": removed}
    expl = [e.to_json_dict() for e in run.adaptations] + [
        r.explanation.to_json_dict() for r in run.repairs
    ]
    rev = PlanRevision(
        season_id=season.id,
        created_at=iso_utc(now_utc()),
        trigger=trigger,
        reason="; ".join(e.headline_zh for e in run.adaptations[:5]) or None,
        horizon_start=run.days[0].date if run.days else None,
        horizon_end=run.days[-1].date if run.days else None,
        before=before,
        after=after,
        diff=run.changes,
        explanation={
            "key": f"plan.revision.{run.today.isoformat()}",
            "items": expl,
            "violations": [v.__dict__ | {"date": v.date.isoformat()} for v in run.violations],
        },
        applied=False,
    )
    s.add(rev)
    s.flush()
    run.revision_id = rev.id
