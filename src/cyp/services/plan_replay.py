"""``cyp plan replay``: re-run readiness and the planner day by day on stored data (read-only).

For every day *D* of ``[start, end]`` (spec adaptive-planning, Phase 0b, decision D8):

1. Readiness of *D* is recomputed from stored wellness, fitness and rides (the profile's
   flags, personal scales learned from rides before *D*), never read from
   ``readiness_daily``.
2. The planner runs as of *D* 05:30 on a view of the stored data in which the planned
   workouts are the replay's own (what the replay planned for each earlier day, as the
   athlete would have seen it that morning), our own calendar events are hidden (only the
   athlete's events count) and readiness is the replay's, up to *D*.
3. The day's plan is frozen, and its plan-vs-actual record is computed with the same
   :func:`cyp.services.compliance.judge_day` the morning run uses (the intent check only for
   key sessions, which the progression gate reads; easy days are judged by load).

Nothing is written: the caller opens the database read-only, no ``planned_workouts``,
``readiness_daily`` or calendar row is touched. Output: one row per day and per week (planned
vs actual, overdone / skipped counts, EASY/REST days, gate outcomes when the gate is on).
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from cyp.analysis.longitudinal.run import load_latest_report
from cyp.analysis.readiness import HARD_CLASSES
from cyp.analysis.readiness_job import readiness_for, ride_response
from cyp.analysis.ride.frames import RideFrame
from cyp.core.ids import external_id
from cyp.dataset import CACHE, PlannedRow, ReadinessRow
from cyp.planning.compliance import KEY_ROLES
from cyp.planning.gate import GateDecision
from cyp.planning.job import PlanRun, plan_horizon
from cyp.planning.planner import DayPlan
from cyp.planning.season import build_skeleton
from cyp.planning.templates import load_library
from cyp.services import adaptive
from cyp.services.compliance import judge_day
from cyp.services.context import AppContext, NoDataError
from cyp.services.readiness import ride_feedback
from cyp.store.models import PlannedWorkout

#: The replay plans each day as of this local time (the autopilot runs at 05:30).
REPLAY_TIME = dt.time(5, 30)
#: Easy-day comparison (Phase 1 acceptance): planned roles that count as easy days.
EASY_ROLES = frozenset({"endurance", "recovery"})


@dataclass
class ReplayDay:
    """One replayed day: readiness, the plan as frozen that morning, what happened."""

    date: dt.date
    readiness: str | None
    readiness_score: float | None
    template_id: str | None
    role: str
    planned_tss: float
    reference_tss: float | None
    actual_tss: float
    rode: bool
    #: Hardest class among the day's analysed rides.
    ride_class: str | None
    compliance: dict[str, Any] | None
    adaptations: list[str] = field(default_factory=list)
    violations: int = 0

    @property
    def overdone(self) -> bool:
        """The plan-vs-actual record says overdone."""
        return bool(self.compliance and self.compliance.get("overdone"))

    @property
    def skipped(self) -> bool:
        """A workout was planned and nothing was ridden."""
        return bool(self.compliance and self.compliance.get("skipped"))


@dataclass
class ReplayWeek:
    """Per-week summary over the replayed days of one Monday-based week."""

    start: dt.date
    days: int
    target_tss: float | None
    planned_tss: float
    actual_tss: float
    ridden: int
    overdone: int
    skipped: int
    easy_rest: int
    gate: dict[str, Any] | None = None
    #: The whole week as planned on its first replayed morning (before daily adaptations).
    planned_first_morning: float | None = None


@dataclass
class ReplayResult:
    """The whole replay (``to_dict`` is the ``--json`` output)."""

    profile: str
    start: dt.date
    end: dt.date
    season_start: dt.date
    flags: dict[str, bool]
    days: list[ReplayDay] = field(default_factory=list)
    weeks: list[ReplayWeek] = field(default_factory=list)

    def easy_days(self) -> dict[str, Any]:
        """Planned vs ridden load on easy days that were ridden easy (Phase 1 acceptance)."""
        pairs = [
            (d.planned_tss, d.actual_tss)
            for d in self.days
            if d.role in EASY_ROLES
            and d.rode
            and d.planned_tss > 0
            and d.ride_class not in HARD_CLASSES
        ]
        if not pairs:
            return {"n": 0}
        planned = statistics.median(p for p, _ in pairs)
        actual = statistics.median(a for _, a in pairs)
        return {
            "n": len(pairs),
            "planned_median": round(planned, 1),
            "actual_median": round(actual, 1),
            "planned_vs_actual": round(planned / actual, 3) if actual else None,
        }

    def totals(self) -> dict[str, Any]:
        """Counts over the whole range."""
        ridden = [d for d in self.days if d.rode]
        return {
            "days": len(self.days),
            "ridden": len(ridden),
            "overdone": sum(d.overdone for d in self.days),
            "overdone_ridden_share": round(sum(d.overdone for d in ridden) / len(ridden), 3)
            if ridden
            else None,
            "skipped": sum(d.skipped for d in self.days),
            "planned_tss": round(sum(d.planned_tss for d in self.days), 1),
            "actual_tss": round(sum(d.actual_tss for d in self.days), 1),
            "violation_days": sum(1 for d in self.days if d.violations),
            "easy_days": self.easy_days(),
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "profile": self.profile,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "season_start": self.season_start.isoformat(),
            "flags": self.flags,
            "totals": self.totals(),
            "weeks": [
                {
                    "start": w.start.isoformat(),
                    "days": w.days,
                    "target_tss": w.target_tss,
                    "planned_tss": w.planned_tss,
                    "actual_tss": w.actual_tss,
                    "ridden": w.ridden,
                    "overdone": w.overdone,
                    "skipped": w.skipped,
                    "easy_rest": w.easy_rest,
                    "planned_first_morning": w.planned_first_morning,
                    "gate": w.gate,
                }
                for w in self.weeks
            ],
            "days": [
                {
                    "date": d.date.isoformat(),
                    "readiness": d.readiness,
                    "readiness_score": d.readiness_score,
                    "template_id": d.template_id,
                    "role": d.role,
                    "planned_tss": d.planned_tss,
                    "reference_tss": d.reference_tss,
                    "actual_tss": d.actual_tss,
                    "rode": d.rode,
                    "ride_class": d.ride_class,
                    "overdone": d.overdone,
                    "skipped": d.skipped,
                    "compliance": d.compliance,
                    "adaptations": d.adaptations,
                    "violations": d.violations,
                }
                for d in self.days
            ],
        }


def _row(day: DayPlan, run: PlanRun, compliance: Mapping[str, Any] | None) -> PlannedRow:
    return PlannedRow(
        date=day.date,
        external_id=external_id(run.season_key, day.date, 1),
        template_id=day.template_id,
        params=dict(day.params),
        role=day.role,
        indoor=not day.outdoor,
        target_tss=round(day.tss, 1),
        status="proposed",
        intent=day.intent,
        reference_tss=run.reference_tss.get(day.date),
        compliance=compliance,
    )


def _transient(day: DayPlan, run: PlanRun) -> PlannedWorkout:
    """A never-added ORM row so the stored-plan code paths can judge the replayed plan."""
    assert day.workout is not None
    return PlannedWorkout(
        template_id=day.template_id,
        name=day.workout.name_zh,
        intent=day.intent,
        steps={
            "params": dict(day.params),
            "role": day.role,
            "outdoor": day.outdoor,
            "reference_tss": run.reference_tss.get(day.date),
        },
        target_tss=round(day.tss, 1),
        indoor=not day.outdoor,
    )


def replay(
    ctx: AppContext,
    start: dt.date,
    end: dt.date,
    *,
    season_start: dt.date | None = None,
) -> ReplayResult:
    """Replay ``[start, end]`` (see the module docstring); writes nothing.

    ``season_start`` moves ``season.start`` for this replay only (to exercise the season
    logic over history from before the real season began).

    Raises:
        NoDataError: no athlete in the store.
        ConfigError: invalid athlete.yaml.
    """
    cfg = ctx.athlete_config()
    if season_start is not None:
        cfg = cfg.model_copy(
            update={"season": cfg.season.model_copy(update={"start": season_start})}
        )
    flags = ctx.features()
    ds0 = CACHE.get(ctx.factory, data_quality=cfg.data_quality.resolved())
    if ds0 is None:
        raise NoDataError("no athlete in the store; run `cyp sync` first")
    # Our own calendar events show the real plan, not the replayed one.
    base = replace(ds0, events=tuple(e for e in ds0.events if not e.ours), planned={})
    feedback = ride_feedback(ctx)
    readiness: dict[dt.date, ReadinessRow] = {}
    bias = _bias(ctx)
    library = load_library()
    sk = build_skeleton(cfg)
    result = ReplayResult(
        profile=ctx.settings.cyp_profile or "default",
        start=start,
        end=end,
        season_start=sk.start,
        flags=flags.as_dict(),
    )
    planned: dict[dt.date, tuple[PlannedRow, ...]] = {}
    targets: dict[dt.date, float] = {}
    gates: dict[dt.date, GateDecision] = {}
    first_morning: dict[dt.date, float] = {}
    gate_on = flags.enabled(adaptive.GATE_FLAG)
    offsets = adaptive.offsets_at(ctx, base) if gate_on else None
    frames: dict[int, RideFrame | None] = {}
    with ctx.factory() as session:
        day = start
        while day <= end:
            # Personal scales as of the day (flag plan.personal_scales), then its readiness.
            scales = adaptive.scales_for(ctx, day, base)
            r0 = readiness_for(base, day, feedback, **adaptive.readiness_inputs(ctx, day, base))
            readiness[day] = ReadinessRow(day, r0.score_0_100, r0.status, r0.recommendation)
            view = replace(
                base,
                planned=dict(planned),
                readiness={d: r for d, r in readiness.items() if d <= day},
            )
            run = plan_horizon(
                view,
                cfg,
                today=day,
                now_local=dt.datetime.combine(day, REPLAY_TIME),
                bias=bias,
                library=library,
                adaptive=adaptive.adaptive_for(scales, gate=gate_on, offsets=offsets),
            )
            for g in run.gate.values():
                gates.setdefault(g.week_start, g)
            monday = day - dt.timedelta(days=day.weekday())
            if monday not in first_morning:
                first_morning[monday] = round(
                    sum(d.tss for d in run.days if monday <= d.date <= monday + dt.timedelta(6)), 1
                )
            for w_start, t in run.week_targets.items():
                if w_start <= day:  # the target as set on the week's own first morning
                    targets.setdefault(w_start, t.target_tss)
            plan = next((d for d in run.days if d.date == day), None)
            rides = [a for a in base.on(day) if a.is_ride]
            record: dict[str, Any] | None = None
            if plan is not None and plan.workout is not None:
                pw = _transient(plan, run)
                record = judge_day(
                    session,
                    ctx.store,
                    base,
                    cfg,
                    day,
                    pw,
                    feedback,
                    with_intent=plan.role in KEY_ROLES,
                    frames=frames,
                )
                planned[day] = (_row(plan, run, record),)
            r = readiness.get(day)
            response = ride_response(base, day)
            result.days.append(
                ReplayDay(
                    date=day,
                    readiness=r.recommendation if r else None,
                    readiness_score=r.score if r else None,
                    template_id=plan.template_id if plan else None,
                    role=plan.role if plan else "rest",
                    planned_tss=round(plan.tss, 1) if plan else 0.0,
                    reference_tss=run.reference_tss.get(day),
                    actual_tss=round(base.loads.get(day, 0.0), 1),
                    rode=bool(rides),
                    ride_class=response.classification if response else None,
                    compliance=record,
                    adaptations=[e.headline_zh for e in run.adaptations],
                    violations=sum(1 for v in run.violations if v.date == day),
                )
            )
            day += dt.timedelta(days=1)
    result.weeks = _weeks(result.days, targets, gates)
    for w in result.weeks:
        w.planned_first_morning = first_morning.get(w.start)
    return result


def _gate_dict(g: GateDecision | None) -> dict[str, Any] | None:
    if g is None:
        return None
    ev = g.evidence
    return {
        "decision": g.decision,
        "progression_index": g.progression_index,
        "season_index": g.season_index,
        "tss_factor": g.tss_factor,
        "recovery": g.recovery,
        "extras_used": g.extras_used,
        "headline_zh": g.explanation.headline_zh if g.explanation else None,
        "evidence": g.explanation.method.inputs if g.explanation and g.explanation.method else None,
        "easy_rest_days_before": ev.easy_rest_days if ev is not None else None,
    }


def _weeks(
    days: list[ReplayDay],
    targets: Mapping[dt.date, float],
    gates: Mapping[dt.date, GateDecision] | None = None,
) -> list[ReplayWeek]:
    by_week: dict[dt.date, list[ReplayDay]] = {}
    for d in days:
        by_week.setdefault(d.date - dt.timedelta(days=d.date.weekday()), []).append(d)
    return [
        ReplayWeek(
            start=w,
            days=len(ds),
            target_tss=targets.get(w),
            planned_tss=round(sum(d.planned_tss for d in ds), 1),
            actual_tss=round(sum(d.actual_tss for d in ds), 1),
            ridden=sum(d.rode for d in ds),
            overdone=sum(d.overdone for d in ds),
            skipped=sum(d.skipped for d in ds),
            easy_rest=sum(1 for d in ds if d.readiness in ("EASY", "REST")),
            gate=_gate_dict((gates or {}).get(w)),
        )
        for w, ds in sorted(by_week.items())
    ]


def _bias(ctx: AppContext) -> dict[str, float]:
    bias = (load_latest_report(ctx.reports_dir) or {}).get("planner_bias", {})
    return {str(k): float(v) for k, v in bias.items()} if isinstance(bias, dict) else {}
