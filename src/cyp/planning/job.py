"""``cyp plan``: build the rolling horizon from the store, adapt, guard, persist (docs/05 §3).

Steps:

1. Season skeleton from ``config/athlete.yaml`` (checkpoint repeats from measured FTP).
2. Horizon ``today .. today + horizon_days - 1`` clipped to the season.
3. Week targets from the CTL going into each week (store for the current week, simulation of
   the planned week for the next).
4. :func:`~cyp.planning.planner.plan_week` per week with limiter bias (trends report),
   availability overrides (icu NOTE ``training_availability=LIMITED`` + ``max_training_time``
   in seconds; HOLIDAY/SICK/INJURED -> 0).
5. Adapt today (calendar, yesterday, readiness) and enforce guardrails on mutable days.
6. Persist: ``seasons`` / ``blocks`` / ``week_plans`` (upsert), ``planned_workouts`` for
   mutable days (``status=proposed``; previous proposals for those dates are superseded),
   and one ``plan_revisions`` row with before/after/diff and the Explanations.

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
from cyp.analysis.longitudinal.run import daily_loads, primary_athlete_id
from cyp.analysis.run import activity_local_date
from cyp.core.errors import AnalysisError
from cyp.core.explain import Explanation
from cyp.core.timeutil import iso_utc, now_utc
from cyp.planning import adapt
from cyp.planning.guardrails import GuardrailInputs, Repair, Violation, enforce
from cyp.planning.planner import DayPlan, PlanContext, Role, plan_week
from cyp.planning.season import SeasonSkeleton, SeasonWeek, build_skeleton, week_targets
from cyp.planning.templates import Template, load_library
from cyp.publish.events import EventSpec, external_id
from cyp.settings import AthleteConfig
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    FitnessDaily,
    IcuEvent,
    PlannedWorkout,
    PlanRevision,
    ReadinessDaily,
    WeekPlan,
)
from cyp.store.models import Block as BlockRow
from cyp.store.models import Season as SeasonRow

SEASON_KEY_FMT = "s{start:%Y%m%d}"
MUTABLE_STATUSES = ("proposed", "published")


def season_key(sk: SeasonSkeleton) -> str:
    """Stable season key used inside ``external_id``."""
    return SEASON_KEY_FMT.format(start=sk.start)


@dataclass
class PlanRun:
    """Result of one ``cyp plan`` run."""

    today: dt.date
    season_key: str
    days: list[DayPlan] = field(default_factory=list)
    week_targets: dict[dt.date, Any] = field(default_factory=dict)
    adaptations: list[Explanation] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    changes: dict[str, list[str]] = field(default_factory=dict)
    revision_id: int | None = None

    @property
    def needs_review(self) -> bool:
        """Guardrails could not be satisfied."""
        return bool(self.violations)


def _overrides_and_events(
    session: Session, start: dt.date, end: dt.date
) -> tuple[dict[dt.date, int], dict[dt.date, list[str]]]:
    rows = session.scalars(
        select(IcuEvent).where(
            IcuEvent.start_date_local >= start.isoformat(),
            IcuEvent.start_date_local < (end + dt.timedelta(days=1)).isoformat(),
        )
    ).all()
    overrides: dict[dt.date, int] = {}
    athlete: dict[dt.date, list[str]] = {}
    for e in rows:
        if (e.external_id or "").startswith("cyp:"):
            continue
        day = dt.date.fromisoformat((e.start_date_local or "")[:10])
        cat = e.category or ""
        if cat == "NOTE" and (e.training_availability or "").upper() == "LIMITED":
            minutes = (e.max_training_time or 0) // 60
            overrides[day] = min(overrides.get(day, minutes), minutes)
        elif cat in adapt.BLOCKING_CATEGORIES:
            overrides[day] = 0
            athlete.setdefault(day, []).append(cat)
        elif cat in adapt.ATHLETE_OWNS:
            athlete.setdefault(day, []).append(cat)
    return overrides, athlete


def _ctl_atl(session: Session, athlete_id: int, day: dt.date) -> tuple[float, float]:
    row = session.get(FitnessDaily, (athlete_id, day))
    if row is None:
        row = session.scalars(
            select(FitnessDaily)
            .where(FitnessDaily.athlete_id == athlete_id, FitnessDaily.date_local <= day)
            .order_by(FitnessDaily.date_local.desc())
        ).first()
    if row is None:
        return 0.0, 0.0
    ctl = row.ctl_icu if row.ctl_icu is not None else row.ctl_sim
    atl = row.atl_icu if row.atl_icu is not None else row.atl_sim
    return float(ctl or 0.0), float(atl or 0.0)


def _longest_ride_min(session: Session, today: dt.date) -> float | None:
    lo = (today - dt.timedelta(weeks=6)).isoformat()
    vals = session.scalars(
        select(Activity.moving_s).where(Activity.is_ride.is_(True), Activity.start_utc >= lo)
    ).all()
    minutes = [int(v) / 60 for v in vals if v]
    return max(minutes) if minutes else None


def _yesterday_facts(
    session: Session, athlete_id: int, today: dt.date, loads: Mapping[dt.date, float]
) -> tuple[bool, float | None, PlannedWorkout | None]:
    y = today - dt.timedelta(days=1)
    lo, hi = (y - dt.timedelta(days=1)).isoformat(), (y + dt.timedelta(days=2)).isoformat()
    rows = session.execute(
        select(Activity, ActivityMetrics)
        .join(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
        .where(Activity.is_ride.is_(True), Activity.start_utc >= lo, Activity.start_utc < hi)
    ).all()
    hard = any(
        activity_local_date(a) == y
        and isinstance(m.pacing, dict)
        and m.pacing.get("classification") in ("sweetspot", "threshold", "vo2", "race")
        for a, m in rows
    )
    planned = session.scalars(
        select(PlannedWorkout).where(
            PlannedWorkout.athlete_id == athlete_id,
            PlannedWorkout.date_local == y,
            PlannedWorkout.status.in_(MUTABLE_STATUSES),
        )
    ).first()
    return hard, loads.get(y), planned


def _dayplan_from_row(row: PlannedWorkout, library: Mapping[str, Template]) -> DayPlan | None:
    from cyp.planning.templates import TemplateError, resolve

    t = library.get(row.template_id or "")
    if t is None:
        return None
    try:
        w = resolve(t, dict((row.steps or {}).get("params", {})), outdoor=not row.indoor)
    except TemplateError:
        return None
    role = cast(Role, str((row.steps or {}).get("role", "hit")))
    return DayPlan(
        date=row.date_local,
        role=role,
        max_minutes=round(w.duration_s / 60),
        template_id=t.id,
        params=dict(w.params),
        outdoor=not row.indoor,
        workout=w,
        intent=t.intent,
    )


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
) -> PlanRun:
    """Plan the horizon starting ``today`` (see module docstring).

    Raises:
        AnalysisError: no athlete in the store.
    """
    lib = dict(library or load_library())
    sk = build_skeleton(cfg)
    key = season_key(sk)
    horizon_end = today + dt.timedelta(days=cfg.planner.horizon_days - 1)
    run = PlanRun(today=today, season_key=key)
    first = max(today, sk.start)
    last = min(horizon_end, sk.weeks[-1].end)
    if first > last:
        return run
    cutoff = cfg.planner.today_cutoff_local
    mutable_from = today if now_local.time() < cutoff else today + dt.timedelta(days=1)

    with factory() as s:
        athlete_id = primary_athlete_id(s)
        if athlete_id is None:
            raise AnalysisError("no athlete in the store; run `cyp sync` first")
        loads, _ = daily_loads(s)
        overrides, athlete_events = _overrides_and_events(s, first, last)
        ctx = PlanContext(
            cfg=cfg, library=lib, bias=dict(bias or {}), overrides=overrides, goal_date=sk.goal_date
        )
        weeks = [w for w in sk.weeks if w.end >= first and w.start <= last]
        ctl, atl = _ctl_atl(s, athlete_id, weeks[0].start - dt.timedelta(days=1))
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
            loads_week = [loads.get(d.date, 0.0) if d.date < today else d.tss for d in days]
            end = simulate(seed, loads_week)[-1]
            ctl, atl = end.ctl, end.atl
            if not w.recovery and w.phase != "test":
                prev_loading = t.target_tss
        horizon = [d for d in all_days if first <= d.date <= last]

        # --- adaptation (today only; calendar for the whole horizon)
        run.adaptations += adapt.apply_calendar(horizon, athlete_events)
        week_today = sk.week_of(today)
        if week_today is not None:
            phase, progress, step = _progress(week_today)
            hard_y, load_y, planned_row = _yesterday_facts(s, athlete_id, today, loads)
            planned_y = _dayplan_from_row(planned_row, lib) if planned_row else None
            run.adaptations += adapt.apply_yesterday(
                horizon, adapt.Yesterday(planned_y, load_y, hard_y), today, ctx, phase, progress
            )
            r = s.get(ReadinessDaily, (athlete_id, today))
            tsb_row = s.get(FitnessDaily, (athlete_id, today - dt.timedelta(days=1)))
            tsb = None
            if tsb_row is not None:
                tsb = tsb_row.tsb_icu if tsb_row.tsb_icu is not None else tsb_row.tsb_sim
            if mutable_from == today:
                run.adaptations += adapt.apply_readiness(
                    horizon,
                    today,
                    r.recommendation if r else None,
                    tsb=tsb,
                    hard_yesterday=hard_y,
                    ctx=ctx,
                    phase=phase,
                    progress=progress,
                    progress_step=step,
                )

        # --- guardrails
        seed_day = first - dt.timedelta(days=1)
        c0, a0 = _ctl_atl(s, athlete_id, seed_day)
        low = frozenset(
            row.date_local
            for row in s.scalars(
                select(ReadinessDaily).where(
                    ReadinessDaily.athlete_id == athlete_id,
                    ReadinessDaily.date_local >= first - dt.timedelta(days=7),
                )
            )
            if (row.score_0_100 or 100) < 40
        )
        gi = GuardrailInputs(
            seed=PMCState(seed_day, c0, a0),
            phase_of=lambda d: (sk.week_of(d) or weeks[0]).phase,
            recovery_weeks=frozenset(w.start for w in sk.weeks if w.recovery or w.phase == "test"),
            history_loads={d: v for d, v in loads.items() if d < first},
            longest_ride_min_6w=_longest_ride_min(s, today),
            low_readiness_days=low,
            ramp_cap=cfg.planner.ramp_cap,
            tsb_floor=cfg.planner.tsb_floor,
            hit_per_week=cfg.planner.hit_per_week,
        )
        run.repairs, run.violations = enforce(horizon, gi, ctx, mutable_from=mutable_from)
        run.days = horizon
        if persist:
            _persist(s, run, sk, athlete_id, cfg, mutable_from, trigger)
            s.commit()
    return run


def _progress(week: SeasonWeek) -> tuple[str, float, float]:
    n_load = 6 if week.phase in ("base", "build", "threshold") else 2
    step = 1 / max(n_load - 1, 1)
    return week.phase, (week.progression_index - 1) * step, step


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
    before: dict[str, dict[str, Any]] = {}
    after: dict[str, dict[str, Any]] = {}
    created: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    for d in run.days:
        if d.date < mutable_from:
            continue
        ext = external_id(run.season_key, d.date, 1)
        existing = s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.external_id == ext)
        ).first()
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


# ------------------------------------------------------------------------------ publish


def event_specs(run: PlanRun, *, render: Any) -> list[EventSpec]:
    """Our desired calendar for the horizon (rest days produce no event)."""
    out: list[EventSpec] = []
    for d in run.days:
        if d.workout is None:
            continue
        footer = ""
        if d.explanation is not None:
            lines = [d.explanation.headline_zh] + [r.text_zh for r in d.explanation.because[:2]]
            footer = "\n\n" + "\n".join(f"# {line}" for line in lines)
        out.append(
            EventSpec(
                external_id=external_id(run.season_key, d.date, 1),
                date=d.date,
                name=d.workout.name_zh,
                description=render(d.workout) + footer,
                indoor=not d.outdoor,
                moving_time_s=d.workout.duration_s,
                target_load=round(d.tss, 1),
                tags=[d.intent, d.role],
            )
        )
    return out
