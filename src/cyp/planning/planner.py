"""Week planner: roles per day -> templates + parameters -> concrete :class:`DayPlan` list.

Deterministic (ADR-0004): same inputs, same plan. Rules (docs/05 §2.2–2.4):

1. **Availability** per weekday from ``config/athlete.yaml``, lowered by calendar overrides
   (athlete NOTE ``max_training_time``, HOLIDAY/SICK/INJURED -> 0). 0 minutes = rest day.
2. **Long ride** on ``planner.long_ride_day`` (fallback: the weekend day with most minutes).
3. **Test** (season test weeks) on Thursday, or the goal date in the final week, with openers the
   day before in the test phase. Tests count as hard days for spacing.
4. **HIT days**: ``week.hit_sessions`` (minus one if a test is scheduled), chosen in the order
   Tue, Thu, Wed, Fri, Sun among days with ≥ :data:`MIN_HIT_MINUTES`, never the day after the
   long ride, never adjacent to another hard day (≥ 48 h).
5. **Templates** by phase menu (:data:`HIT_MENU`, :data:`LONG_MENU`) and progression index;
   alternatives inside a menu slot are ranked by the limiter bias (``intent -> weight``).
   Progression params grow in the template's ``progression`` order; values that do not fit the
   day's minutes step back.
6. **Residual** TSS (week target − fixed sessions) is spread over the remaining days in
   proportion to their minutes, each filled with the Z2 / cadence / recovery template closest
   to its share; a share < :data:`MIN_RESIDUAL_TSS` becomes a rest day.

Every day carries an :class:`~cyp.core.explain.Explanation` (key ``plan.day.YYYY-MM-DD``).
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.season import PHASE_ZH, SeasonWeek, WeekTargets
from cyp.planning.templates import ResolvedWorkout, Template, TemplateError, resolve
from cyp.settings import WEEKDAYS, AthleteConfig

PLANNER_VERSION = "planner_v1"
Role = Literal["rest", "recovery", "endurance", "long_ride", "hit", "test", "opener"]
HARD_ROLES = frozenset({"hit", "test"})
HARD_INTENTS = frozenset({"sweetspot", "threshold", "vo2", "anaerobic", "test"})
MIN_HIT_MINUTES = 60
MIN_LONG_MINUTES = 120
MIN_RESIDUAL_TSS = 15.0
Z2_TSS_PER_MIN = 0.66**2 * 100 / 60
HIT_DAY_ORDER = ("tue", "thu", "wed", "fri", "sun", "sat", "mon")
ROLE_VALUE: dict[str, float] = {
    "test": 1.0,
    "long_ride": 0.9,
    "hit": 0.8,
    "opener": 0.6,
    "endurance": 0.3,
    "recovery": 0.1,
    "rest": 0.0,
}
ROLE_ZH: dict[str, str] = {
    "rest": "休息",
    "recovery": "恢復騎",
    "endurance": "Z2 耐力",
    "long_ride": "長騎",
    "hit": "高強度",
    "test": "測驗",
    "opener": "開腿",
}

#: Hard-session menu per phase: one list per weekly HIT slot; each entry is a list of
#: progression stages, each stage a tuple of alternative template ids (bias picks one).
HIT_MENU: dict[str, list[list[tuple[str, ...]]]] = {
    "base": [[("ss_3x_n",)]],
    "build": [
        [
            ("threshold_3x12",),
            ("threshold_3x12",),
            ("threshold_2x15",),
            ("threshold_2x20",),
            ("threshold_2x20",),
            ("threshold_3x20",),
        ],
        [
            ("ss_2x20",),
            ("ss_3x20",),
            ("ss_3x20",),
            ("over_unders_3x_n", "climb_repeats_goal_power"),
            ("over_unders_3x_n", "climb_repeats_goal_power"),
            ("over_unders_3x_n", "climb_repeats_goal_power"),
        ],
    ],
    # Alternating weeks: odd progression -> VO2 first, even -> threshold first.
    "threshold": [
        [
            ("vo2_4x5",),
            ("threshold_2x20",),
            ("vo2_4x5",),
            ("threshold_2x20",),
            ("vo2_5x5",),
            ("threshold_2x20",),
        ],
        [
            ("climb_repeats_goal_power", "threshold_2x15"),
            ("vo2_40_20", "vo2_30_30_2sets"),
            ("climb_repeats_goal_power", "threshold_2x15"),
            ("vo2_5x3", "vo2_40_20"),
            ("climb_repeats_goal_power", "over_unders_3x_n"),
            ("vo2_5x3", "vo2_30_30_2sets"),
        ],
    ],
    "test": [[("vo2_5x3",)]],
}
LONG_MENU: dict[str, tuple[str, ...]] = {
    "base": ("long_ride_late_tempo", "z2_endurance_240", "z2_endurance_180"),
    "build": ("long_ride_late_ss", "long_ride_late_tempo", "z2_endurance_180"),
    "threshold": ("long_ride_late_ss", "z2_endurance_180"),
    "test": ("z2_endurance_120",),
}
RECOVERY_LONG = ("z2_endurance_120",)
ENDURANCE_POOL = (
    "z2_endurance_60",
    "z2_endurance_90",
    "z2_endurance_120",
    "z2_endurance_180",
    "z2_cadence_90",
    "z2_cadence_120",
    "recovery_spin",
)
TEST_TEMPLATE = {"ramp": "ramp_test", "twenty_min": "ftp_test_20min"}


@dataclass
class DayPlan:
    """One planned day (``template_id`` ``None`` = rest)."""

    date: dt.date
    role: Role
    max_minutes: int
    template_id: str | None = None
    params: dict[str, int] = field(default_factory=dict)
    outdoor: bool = True
    workout: ResolvedWorkout | None = None
    explanation: Explanation | None = None
    notes: list[str] = field(default_factory=list)
    intent: str = "rest"  # template intent, "rest" without a workout

    @property
    def tss(self) -> float:
        """Closed-form TSS (0 for rest)."""
        return self.workout.tss if self.workout else 0.0

    @property
    def minutes(self) -> int:
        """Planned minutes (0 for rest)."""
        return round(self.workout.duration_s / 60) if self.workout else 0

    @property
    def is_hard(self) -> bool:
        """HIT or test (counts for hit spacing / hit_per_week)."""
        return self.role in HARD_ROLES or self.intent in HARD_INTENTS

    @property
    def value(self) -> float:
        """Repair priority: lower is removed first (docs/05 §5)."""
        return ROLE_VALUE[self.role]

    def set_rest(self, reason_zh: str) -> None:
        """Turn the day into rest, keeping the reason."""
        self.role, self.template_id, self.params, self.workout, self.intent = (
            "rest",
            None,
            {},
            None,
            "rest",
        )
        self.notes.append(reason_zh)


@dataclass
class PlanContext:
    """Everything the planner needs that is not the week itself."""

    cfg: AthleteConfig
    library: Mapping[str, Template]
    bias: Mapping[str, float] = field(default_factory=dict)
    overrides: Mapping[dt.date, int] = field(default_factory=dict)  # max minutes per date
    indoor_days: frozenset[dt.date] = frozenset()
    goal_date: dt.date | None = None


def weekday_key(day: dt.date) -> str:
    """``mon``..``sun``."""
    return WEEKDAYS[day.weekday()]


def availability(week: SeasonWeek, ctx: PlanContext) -> dict[dt.date, int]:
    """Minutes per date: config per weekday, lowered by overrides."""
    out: dict[dt.date, int] = {}
    for i in range(7):
        day = week.start + dt.timedelta(days=i)
        minutes = ctx.cfg.availability.minutes_for(weekday_key(day))  # type: ignore[arg-type]
        if day in ctx.overrides:
            minutes = min(minutes, ctx.overrides[day])
        out[day] = max(minutes, 0)
    return out


# ------------------------------------------------------------------------------- roles


def assign_roles(
    week: SeasonWeek, avail: Mapping[dt.date, int], ctx: PlanContext
) -> dict[dt.date, Role]:
    """Role per day (rules 2–4 of the module docstring)."""
    days = sorted(avail)
    roles: dict[dt.date, Role] = {d: ("endurance" if avail[d] > 0 else "rest") for d in days}
    by_key = {weekday_key(d): d for d in days}

    long_day = by_key.get(ctx.cfg.planner.long_ride_day)
    if long_day is None or avail[long_day] < MIN_LONG_MINUTES:
        weekend = [d for d in days if d.weekday() >= 5 and avail[d] >= MIN_LONG_MINUTES]
        long_day = max(weekend, key=lambda d: (avail[d], -d.toordinal()), default=None)
    if long_day is not None:
        roles[long_day] = "long_ride"
    after_long = long_day + dt.timedelta(days=1) if long_day else None

    hard: list[dt.date] = []

    def ok_for_hard(d: dt.date) -> bool:
        if avail.get(d, 0) < MIN_HIT_MINUTES or roles[d] in ("long_ride", "rest"):
            return False
        if d == after_long:
            return False
        return all(abs((d - h).days) >= 2 for h in hard)

    if week.test:
        target = None
        if ctx.goal_date and week.start <= ctx.goal_date <= week.end and week.phase == "test":
            target = ctx.goal_date
        candidates = [target] if target else []
        candidates += [by_key[k] for k in ("thu", "wed", "fri", "tue") if k in by_key]
        test_day = next((d for d in candidates if d is not None and ok_for_hard(d)), None)
        if test_day is not None:
            roles[test_day] = "test"
            hard.append(test_day)
            eve = test_day - dt.timedelta(days=1)
            if week.phase == "test" and eve in roles and roles[eve] == "endurance":
                roles[eve] = "opener"
    n_hit = max(week.hit_sessions - (1 if week.test else 0), 0)
    if week.phase == "test" and week.test:
        n_hit = 0
    candidates = [
        by_key[k]
        for k in HIT_DAY_ORDER
        if k in by_key and roles[by_key[k]] == "endurance" and ok_for_hard(by_key[k])
    ]
    # First combination (in preference order) whose days are pairwise ≥ 48 h apart; if no set
    # of n fits, try n - 1 (never squeeze hard days together).
    for n in range(min(n_hit, len(candidates)), 0, -1):
        combo = next(
            (
                c
                for c in itertools.combinations(candidates, n)
                if all(abs((a - b).days) >= 2 for a, b in itertools.combinations(c, 2))
            ),
            None,
        )
        if combo is not None:
            for d in combo:
                roles[d] = "hit"
            break
    return roles


# --------------------------------------------------------------------------- templates


def progression_sequence(t: Template) -> list[dict[str, int]]:
    """Param dicts in progression order: grow ``progression[0]`` from its min, then the next."""
    base = t.defaults()
    if not t.progression:
        return [base]
    seq: list[dict[str, int]] = []
    cur = dict(base)
    first = t.progression[0]
    cur[first] = t.params[first].min
    seq.append(dict(cur))
    for name in t.progression:
        spec = t.params[name]
        while cur[name] + spec.step <= spec.max:
            cur[name] += spec.step
            seq.append(dict(cur))
    return seq


def pick_params(
    t: Template, progress: float, *, max_minutes: int, outdoor: bool
) -> tuple[dict[str, int], ResolvedWorkout] | None:
    """Params at ``progress`` ∈ [0, 1] along the sequence, stepping back until it fits."""
    seq = progression_sequence(t)
    idx = round(min(max(progress, 0.0), 1.0) * (len(seq) - 1))
    for i in range(idx, -1, -1):
        try:
            w = resolve(t, seq[i], outdoor=outdoor)
        except TemplateError:
            continue
        if w.duration_s <= max_minutes * 60:
            return seq[i], w
    return None


def _venue(t: Template, day: dt.date, ctx: PlanContext, role: Role) -> bool | None:
    """Outdoor? (outdoor-first; indoor for indoor-only templates, rain days and indoor tests)."""
    wants_indoor = day in ctx.indoor_days or (
        role == "test" and ctx.cfg.location.test_venue == "indoor"
    )
    if ctx.cfg.location.indoor_policy == "always":
        wants_indoor = True
    if wants_indoor and t.indoor_ok:
        return False
    if t.outdoor_ok and ctx.cfg.location.indoor_policy != "always":
        return True
    return False if t.indoor_ok else None


def eligible(t: Template, phase: str) -> bool:
    """Template usable in ``phase``.

    The test phase also draws on threshold-phase templates (its taper keeps Z2 and short VO2
    touches).
    """
    return phase in t.phases or (phase == "test" and "threshold" in t.phases)


def _rank(options: Sequence[str], ctx: PlanContext, phase: str) -> list[str]:
    usable = [o for o in options if o in ctx.library and eligible(ctx.library[o], phase)]
    return sorted(usable, key=lambda o: -ctx.bias.get(ctx.library[o].intent, 1.0))


def _fill(
    day: DayPlan, options: Sequence[str], ctx: PlanContext, phase: str, progress: float
) -> bool:
    for tid in _rank(options, ctx, phase):
        t = ctx.library[tid]
        outdoor = _venue(t, day.date, ctx, day.role)
        if outdoor is None:
            continue
        got = pick_params(t, progress, max_minutes=day.max_minutes, outdoor=outdoor)
        if got is None:
            continue
        day.template_id, day.outdoor = tid, outdoor
        day.params, day.workout = got
        day.intent = t.intent
        return True
    return False


def fill_endurance(
    day: DayPlan, target_tss: float, ctx: PlanContext, phase: str, prefer_cadence: bool
) -> bool:
    """Put the endurance / recovery template closest to ``target_tss`` that fits the day."""
    best: tuple[float, str, ResolvedWorkout, bool] | None = None
    for tid in ENDURANCE_POOL:
        t = ctx.library.get(tid)
        if t is None or not eligible(t, phase):
            continue
        outdoor = _venue(t, day.date, ctx, "endurance")
        if outdoor is None:
            continue
        try:
            w = resolve(t, None, outdoor=outdoor)
        except TemplateError:
            continue
        if w.duration_s > day.max_minutes * 60:
            continue
        cost = abs(w.tss - target_tss) - (3.0 if prefer_cadence and "cadence" in tid else 0.0)
        if best is None or cost < best[0]:
            best = (cost, tid, w, outdoor)
    if best is None:
        return False
    _, tid, w, outdoor = best
    day.template_id, day.workout, day.params, day.outdoor = tid, w, dict(w.params), outdoor
    day.intent = ctx.library[tid].intent
    day.role = "recovery" if ctx.library[tid].intent == "recovery" else "endurance"
    return True


def plan_week(week: SeasonWeek, targets: WeekTargets, ctx: PlanContext) -> list[DayPlan]:
    """Concrete days for one season week."""
    avail = availability(week, ctx)
    roles = assign_roles(week, avail, ctx)
    days = {d: DayPlan(date=d, role=roles[d], max_minutes=avail[d]) for d in sorted(avail)}
    n_load = 6 if week.phase in ("base", "build", "threshold") else 2
    progress = (week.progression_index - 1) / max(n_load - 1, 1)
    menu = HIT_MENU.get(week.phase, [])
    hit_days = [d for d in sorted(days) if days[d].role == "hit"]
    for slot, d in enumerate(hit_days):
        stages = menu[slot % len(menu)] if menu else []
        if not stages:
            days[d].role = "endurance"
            continue
        single = len(stages) == 1
        at = min(week.progression_index - 1, len(stages) - 1)
        # The stage for this week; if none of its templates fits, fall back to earlier stages.
        if not any(
            _fill(days[d], stages[i], ctx, week.phase, progress if single else 0.5)
            for i in range(at, -1, -1)
        ):
            days[d].role = "endurance"
            days[d].notes.append("沒有可用的高強度模板放得進這天的時間，改為 Z2")
    for day in days.values():
        if day.role == "long_ride":
            menu_long = RECOVERY_LONG if week.recovery else LONG_MENU.get(week.phase, ())
            if not _fill(day, menu_long, ctx, week.phase, progress):
                day.role = "endurance"
        elif day.role == "test":
            tid = TEST_TEMPLATE[week.test or "twenty_min"]
            if not _fill(day, (tid,), ctx, week.phase, 0.0):
                day.role = "endurance"
        elif day.role == "opener" and not _fill(day, ("openers",), ctx, week.phase, 0.0):
            day.role = "endurance"
    fixed = sum(day.tss for day in days.values() if day.workout is not None)
    residual = max(targets.target_tss - fixed, 0.0)
    open_days = [
        d
        for d in sorted(days)
        if days[d].role in ("endurance", "recovery") and days[d].workout is None
    ]
    total_min = sum(avail[d] for d in open_days) or 1
    cadence_used = False
    for d in open_days:
        share = residual * avail[d] / total_min
        share = min(share, avail[d] * Z2_TSS_PER_MIN)
        if share < MIN_RESIDUAL_TSS:
            days[d].set_rest(
                f"本週剩餘負荷分到這天只有 {share:.0f} TSS，低於 {MIN_RESIDUAL_TSS:.0f}，休息"
            )
            continue
        prefer_cadence = week.phase in ("base", "build") and not cadence_used
        if fill_endurance(days[d], share, ctx, week.phase, prefer_cadence):
            cadence_used = cadence_used or "cadence" in (days[d].template_id or "")
        else:
            days[d].set_rest("沒有放得進這天時間的耐力模板")
    for day in days.values():
        if day.role == "rest" and not day.notes:
            day.notes.append("這天沒有可用時間" if day.max_minutes == 0 else "休息日")
        day.explanation = explain_day(day, week, targets, ctx)
    return [days[d] for d in sorted(days)]


# ------------------------------------------------------------------------- explanation


def explain_day(
    day: DayPlan, week: SeasonWeek, targets: WeekTargets, ctx: PlanContext
) -> Explanation:
    """Why this workout on this day (docs/07 §1)."""
    because: list[Reason] = []
    t = ctx.library.get(day.template_id or "")
    wk = f"第 {week.index} 週 {PHASE_ZH[week.phase]}" + ("（恢復週）" if week.recovery else "")
    if t is None or day.workout is None:
        headline = f"{day.date.isoformat()} 休息（{wk}）"
        for n in day.notes:
            because.append(Reason(text_zh=n, evidence={"max_minutes": day.max_minutes}))
    else:
        w = day.workout
        venue = "室外" if day.outdoor else "室內"
        headline = (
            f"{day.date.isoformat()} {w.name_zh}（{ROLE_ZH[day.role]}，{venue}，"
            f"約 {w.tss:.0f} TSS／{round(w.duration_s / 60)} 分）"
        )
        # Swap / adaptation reasons first: the calendar footer shows only the first two.
        for n in day.notes:
            because.append(Reason(text_zh=n, evidence={}))
        because.append(
            Reason(text_zh=t.explain_zh, evidence={"template_id": t.id, "version": t.version})
        )
        because.append(
            Reason(
                text_zh=(f"{wk}的{ROLE_ZH[day.role]}日；這天可用 {day.max_minutes} 分鐘"),
                evidence={
                    "role": day.role,
                    "max_minutes": day.max_minutes,
                    "progression_index": week.progression_index,
                },
            )
        )
        if t.progression:
            because.append(
                Reason(
                    text_zh=f"進程第 {week.progression_index} 步：參數 {w.params}",
                    evidence={"params": dict(w.params)},
                )
            )
        b = ctx.bias.get(t.intent)
        if b and abs(b - 1.0) > 1e-6:
            because.append(
                Reason(
                    text_zh=f"限制因子偏好讓 {t.intent} 類課權重 ×{b:.2f}",
                    evidence={"intent": t.intent, "bias": b},
                )
            )
        if not day.outdoor:
            reason = (
                "測驗在室內做比較可重複"
                if day.role == "test"
                else "預報不適合戶外"
                if day.date in ctx.indoor_days
                else "此模板只有室內版"
            )
            because.append(Reason(text_zh=f"室內：{reason}", evidence={"outdoor": False}))
    because.append(
        Reason(
            text_zh=f"本週目標 {targets.target_tss:.0f} TSS、約 {targets.target_hours:.1f} 小時",
            evidence={
                "week_target_tss": targets.target_tss,
                "week_target_hours": targets.target_hours,
            },
        )
    )
    return Explanation(
        key=f"plan.day.{day.date.isoformat()}",
        headline_zh=headline,
        because=because,
        method=MethodRef(
            model_id="planner",
            version=PLANNER_VERSION,
            inputs={
                "phase": week.phase,
                "week": week.index,
                "role": day.role,
                "bias": dict(ctx.bias),
            },
            doc="docs/05-training-engine.md",
        ),
        confidence="medium",
        glossary_terms=["periodization_3_1", "workout_library", "guardrails"],
    )


def summarize(days: Sequence[DayPlan]) -> dict[str, Any]:
    """Totals for a list of days."""
    return {
        "tss": round(sum(d.tss for d in days), 1),
        "minutes": sum(d.minutes for d in days),
        "hard_days": [d.date.isoformat() for d in days if d.is_hard],
        "rest_days": [d.date.isoformat() for d in days if d.role == "rest"],
    }
