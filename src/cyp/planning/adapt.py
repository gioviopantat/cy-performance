"""Daily adaptation of the horizon (docs/05 §3 steps 1, 2 and 4).

Applied to freshly planned days before the guardrails:

- **Calendar collisions**: athlete-created events win. ``HOLIDAY`` / ``SICK`` / ``INJURED``
  -> no workout; a ``RACE_*`` or the athlete's own ``WORKOUT`` on a day -> we leave that day
  to them. (Limited-availability NOTEs are applied earlier as minute overrides.)
- **Yesterday**: a missed hard session is re-slotted within 72 h onto an endurance day that
  keeps 48 h from other hard days, else dropped (never stacked); a ride > 130 % of its plan
  turns today into recovery. A day the athlete tagged casual counts as not done (a hard plan is
  re-slotted whatever was ridden) and is never overdone (spec casual-and-strava).
- **Easy days** (flag ``plan.personal_scales``, :func:`apply_easy_days`): an easy day below the
  athlete's learned easy-day load is planned as a 60–90 min Z2 ride near it (IF ≤ 0.70).
- **Readiness today**: ``REST`` -> rest (a hard session moves ≥ 48 h later if possible);
  ``EASY`` -> at most Z2 at 60 % of the planned TSS; ``UPGRADE`` -> one progression step more on
  a hard day if TSB > −10 and yesterday was not hard (never adds a new hard session);
  ``AS_PLANNED`` -> unchanged.

Every change appends an Explanation to the returned list (key ``plan.adapt.<what>.<date>``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass

from cyp.analysis.personal_scales import Learned
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance, pick_params
from cyp.planning.templates import Template, load_library, resolve

ADAPT_VERSION = "adapt_v1"
BLOCKING_CATEGORIES = frozenset({"HOLIDAY", "SICK", "INJURED"})
ATHLETE_OWNS = frozenset({"RACE_A", "RACE_B", "RACE_C", "WORKOUT"})
EASY_FACTOR = 0.6
#: A day counts as overdone when its load exceeds both this share of the slot's target ...
OVERDONE_FACTOR = 1.3
#: ... and the target plus this many TSS (a 19-TSS recovery spin ridden as a 50-TSS spin is not
#: "overdone" in any way that matters; this stops the old recovery -> overdone -> recovery loop).
OVERDONE_MARGIN_TSS = 40.0
#: Next day's target drops by this share of yesterday's excess load ...
OVERDONE_CARRY = 0.5
#: ... but never below this share of what was planned for it.
OVERDONE_FLOOR = 0.6
#: A very large excess (more than this many TSS over the target) caps the next day at an easy
#: endurance ride of about this load (Z2 ~60 min); recovery spins come only from readiness.
OVERDONE_LARGE_TSS = 100.0
OVERDONE_EASY_DAY_TSS = 45.0


def is_overdone(actual: float | None, target: float | None) -> bool:
    """Whether ``actual`` overshoots ``target`` by both factor and margin.

    The one rule the planner and the reports share (:data:`OVERDONE_FACTOR`,
    :data:`OVERDONE_MARGIN_TSS`).
    """
    if actual is None or not target or target <= 0:
        return False
    return actual > max(OVERDONE_FACTOR * target, target + OVERDONE_MARGIN_TSS)


RESLOT_WINDOW_DAYS = 3


@dataclass(frozen=True)
class Yesterday:
    """What happened to yesterday's planned session."""

    planned: DayPlan | None
    actual_load: float | None
    completed_hard: bool  # an analysed ride classified hard happened yesterday
    #: The slot's target before any adaptation (the season's plan for that day). Overdone is
    #: judged against it, not against a recovery spin an earlier adaptation put there.
    reference_tss: float | None = None
    #: Every ride of the day was tagged casual by the athlete: the plan was not done.
    casual: bool = False


def _expl(kind: str, day: dt.date, headline: str, reasons: list[Reason]) -> Explanation:
    return Explanation(
        key=f"plan.adapt.{kind}.{day.isoformat()}",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id=f"adapt.{kind}",
            version=ADAPT_VERSION,
            inputs={},
            doc="docs/05-training-engine.md",
        ),
        confidence="high",
        glossary_terms=["readiness_v1", "guardrails"],
    )


def apply_calendar(
    days: Sequence[DayPlan], athlete_events: Mapping[dt.date, Sequence[str]]
) -> list[Explanation]:
    """Clear our workout on days the athlete blocked or claimed."""
    out: list[Explanation] = []
    for d in days:
        cats = set(athlete_events.get(d.date, ()))
        hit = cats & (BLOCKING_CATEGORIES | ATHLETE_OWNS)
        if not hit or d.workout is None:
            continue
        before = d.template_id
        label = "、".join(sorted(hit))
        d.set_rest(f"你在 icu 日曆上這天有 {label}，以你的安排為準")
        out.append(
            _expl(
                "calendar",
                d.date,
                f"{d.date.isoformat()}：{before} 取消（{label}）",
                [
                    Reason(
                        text_zh="你自己建立的事件優先於系統課表",
                        evidence={"categories": sorted(hit)},
                    )
                ],
            )
        )
    return out


def _hard_ok(days: Sequence[DayPlan], day: DayPlan) -> bool:
    return all(abs((o.date - day.date).days) >= 2 for o in days if o.is_hard and o is not day)


def reslot_missed_hard(
    days: list[DayPlan],
    missed: DayPlan,
    today: dt.date,
    ctx: PlanContext,
    phase: str,
    progress: float,
) -> list[Explanation]:
    """Move a missed hard session to an endurance day within 72 h, else drop it."""
    for d in sorted(days, key=lambda x: x.date):
        if not (today <= d.date <= missed.date + dt.timedelta(days=RESLOT_WINDOW_DAYS)):
            continue
        if d.role != "endurance" or d.workout is None or d.max_minutes < missed.minutes:
            continue
        probe = DayPlan(date=d.date, role="hit", max_minutes=d.max_minutes)
        if not _hard_ok(days, probe):
            continue
        t = ctx.library.get(missed.template_id or "")
        if t is None:
            break
        got = pick_params(t, progress, max_minutes=d.max_minutes, outdoor=missed.outdoor)
        if got is None:
            continue
        d.role, d.template_id, d.outdoor, d.intent = "hit", t.id, missed.outdoor, t.intent
        d.params, d.workout = got
        d.notes.append(f"{missed.date.isoformat()} 沒做的 {t.name_zh} 移到這天")
        return [
            _expl(
                "reslot",
                d.date,
                f"{t.name_zh} 從 {missed.date.isoformat()} 移到 {d.date.isoformat()}",
                [
                    Reason(
                        text_zh="漏掉的高強度在 72 小時內找得到間隔 48 小時的空檔就補",
                        evidence={"missed": missed.date.isoformat()},
                    )
                ],
            )
        ]
    return [
        _expl(
            "drop",
            missed.date,
            f"{missed.date.isoformat()} 的高強度不補了",
            [
                Reason(
                    text_zh="72 小時內找不到與其他高強度間隔 48 小時的日子；不疊課",
                    evidence={"missed": missed.date.isoformat()},
                )
            ],
        )
    ]


def apply_yesterday(
    days: list[DayPlan],
    y: Yesterday,
    today: dt.date,
    ctx: PlanContext,
    phase: str,
    progress: float,
) -> list[Explanation]:
    """Compliance rules for yesterday's slot."""
    out: list[Explanation] = []
    p = y.planned
    if p is None or p.workout is None:
        return out
    if p.is_hard and (y.casual or (not y.completed_hard and (y.actual_load or 0) < 0.5 * p.tss)):
        out += reslot_missed_hard(days, p, today, ctx, phase, progress)
    if y.casual:
        return out
    ref = max(p.tss, y.reference_tss or 0.0)
    if is_overdone(y.actual_load, ref):
        assert y.actual_load is not None
        t = next((d for d in days if d.date == today), None)
        if t is not None and t.workout is not None:
            out.append(_ease_after_overdone(t, y.actual_load, ref, today, ctx, phase))
    return out


def _ease_after_overdone(
    t: DayPlan, actual: float, ref: float, today: dt.date, ctx: PlanContext, phase: str
) -> Explanation:
    """Take half of yesterday's excess off today, keep it an endurance ride, never a spin.

    A hard session becomes endurance (no second hard day on tired legs); a large excess caps
    the day at an easy hour of Z2. Readiness can still lower it further from body signals.
    """
    excess = actual - ref
    before, before_tss, was_hard = t.template_id, t.tss, t.is_hard
    target = max(before_tss - OVERDONE_CARRY * excess, OVERDONE_FLOOR * before_tss)
    if excess > OVERDONE_LARGE_TSS:
        target = min(target, OVERDONE_EASY_DAY_TSS)
    changed = was_hard or target < before_tss - 1
    if changed:
        fill_endurance(t, target, ctx, phase, prefer_cadence=False)
        t.notes.append(f"昨天多騎了 {excess:.0f} TSS，今天減量")
    reasons = [
        Reason(
            text_zh=f"昨天實際 {actual:.0f} TSS，原本這天排 {ref:.0f}（多 {excess:.0f}）",
            evidence={"actual": round(actual), "reference": round(ref), "excess": round(excess)},
        ),
        Reason(
            text_zh=(
                f"今天扣掉多出負荷的 {OVERDONE_CARRY:.0%}，最少保留原計畫的 {OVERDONE_FLOOR:.0%}"
                + (
                    "；強度課改成耐力騎"
                    if before_tss and t.intent != "recovery" and changed
                    else ""
                )
                + (
                    f"；多出超過 {OVERDONE_LARGE_TSS:.0f} TSS，今天最多一小時輕鬆 Z2"
                    if excess > OVERDONE_LARGE_TSS
                    else ""
                )
            ),
            evidence={
                "carry": OVERDONE_CARRY,
                "floor": OVERDONE_FLOOR,
                "large_tss": OVERDONE_LARGE_TSS,
                "target_tss": round(target),
            },
        ),
    ]
    return _expl(
        "overdone",
        today,
        f"{today.isoformat()}：{before} → {t.template_id}"
        if changed
        else f"{today.isoformat()}：照計畫",
        reasons,
    )


def apply_readiness(
    days: list[DayPlan],
    today: dt.date,
    recommendation: str | None,
    *,
    tsb: float | None,
    hard_yesterday: bool,
    ctx: PlanContext,
    phase: str,
    progress: float,
    progress_step: float,
) -> list[Explanation]:
    """Readiness rules for today's slot."""
    d = next((x for x in days if x.date == today), None)
    if d is None or recommendation in (None, "AS_PLANNED") or d.workout is None:
        return []
    before, tss_before = d.template_id, d.tss
    if recommendation == "REST":
        moved: list[Explanation] = []
        if d.is_hard:
            missed = DayPlan(
                date=d.date,
                role=d.role,
                max_minutes=d.max_minutes,
                template_id=d.template_id,
                params=dict(d.params),
                outdoor=d.outdoor,
                workout=d.workout,
                intent=d.intent,
            )
            d.set_rest("準備度：休息")
            moved = reslot_missed_hard(
                days, missed, today + dt.timedelta(days=2), ctx, phase, progress
            )
        else:
            d.set_rest("準備度：休息")
        return [
            _expl(
                "readiness",
                today,
                f"今天 {before} → 休息（準備度 REST）",
                [Reason(text_zh="準備度判定今天休息，覆蓋一切課表", evidence={})],
            ),
            *moved,
        ]
    if recommendation == "EASY":
        if d.is_hard or d.role == "long_ride" or d.tss > 0:
            fill_endurance(d, EASY_FACTOR * tss_before, ctx, phase, prefer_cadence=False)
            d.notes.append("準備度 EASY：只騎 Z2，負荷不超過原計畫 60 %")
            return [
                _expl(
                    "readiness",
                    today,
                    f"今天 {before} → {d.template_id}（準備度 EASY）",
                    [
                        Reason(
                            text_zh=(
                                f"原計畫 {tss_before:.0f} TSS，上限 {EASY_FACTOR * tss_before:.0f}"
                            ),
                            evidence={
                                "planned_tss": round(tss_before),
                                "cap": round(EASY_FACTOR * tss_before),
                            },
                        )
                    ],
                )
            ]
        return []
    if recommendation == "UPGRADE" and d.is_hard and d.role == "hit":
        if (tsb is not None and tsb <= -10) or hard_yesterday:
            return []
        t = ctx.library.get(d.template_id or "")
        if t is None:
            return []
        got = pick_params(t, progress + progress_step, max_minutes=d.max_minutes, outdoor=d.outdoor)
        if got is None or got[0] == d.params:
            return []
        d.params, d.workout = got
        d.notes.append("準備度 UPGRADE：進程 +1 步")
        return [
            _expl(
                "readiness",
                today,
                f"今天 {t.name_zh} 進程 +1（準備度 UPGRADE）",
                [
                    Reason(
                        text_zh="TSB > −10 且昨天不是高強度，允許多一步進程，不加新的高強度",
                        evidence={"tsb": tsb, "params": dict(got[0])},
                    )
                ],
            )
        ]
    return []


#: Easy days planned from the athlete's habit (decision D2) are Z2 rides of 60–90 min ...
EASY_DAY_POOL = ("z2_endurance_60", "z2_endurance_90", "z2_cadence_90")
#: ... whose intensity stays explicitly capped (IF), so a longer easy day is still easy.
EASY_DAY_IF_CAP = 0.70
#: Athletes 60+ follow their habit too (Roger, 2026-10-17) but with a lower cap: plain Z2
#: (the easy-day templates sit at IF ~0.66), nothing harder; longer, not harder.
MASTERS_EASY_DAY_IF_CAP = 0.67


def easy_day_default_tss(library: Mapping[str, Template] | None = None) -> float:
    """The template default an easy day is shrunk to: the first easy-pool ride's load (Z2 60)."""
    lib = library if library is not None else load_library()
    return resolve(lib[EASY_DAY_POOL[0]], None, outdoor=True).tss


def apply_easy_days(
    days: Sequence[DayPlan],
    easy: Learned,
    *,
    mutable_from: dt.date,
    skip: Collection[dt.date],
    ahead_weeks: Collection[dt.date],
    ctx: PlanContext,
    phase_of: Callable[[dt.date], str],
) -> list[Explanation]:
    """Plan easy days near the athlete's own easy-day load (decision D2, ADR-0019).

    An endurance / recovery day below the learned ``easy_day_tss`` becomes the
    :data:`EASY_DAY_POOL` ride closest to it (IF ≤ :data:`EASY_DAY_IF_CAP`, within the day's
    minutes); never lower than planned. Skipped: frozen days, ``skip`` (today when readiness is
    below AS_PLANNED or another adaptation already changed it), days in ``ahead_weeks`` (the
    athlete is already ahead of the week's plan) and a value that is only the default (no
    learned habit yet). Masters (≥ 60) get the habit too, capped at
    :data:`MASTERS_EASY_DAY_IF_CAP`.
    """
    if easy.source == "default":
        return []
    if_cap = MASTERS_EASY_DAY_IF_CAP if ctx.masters else EASY_DAY_IF_CAP
    out: list[Explanation] = []
    for d in days:
        week = d.date - dt.timedelta(days=d.date.weekday())
        if d.date < mutable_from or d.date in skip or week in ahead_weeks:
            continue
        if d.workout is None or d.is_hard or d.role not in ("endurance", "recovery"):
            continue
        if d.tss >= easy.value:
            continue
        probe = DayPlan(date=d.date, role="endurance", max_minutes=d.max_minutes)
        if not fill_endurance(
            probe,
            easy.value,
            ctx,
            phase_of(d.date),
            prefer_cadence=False,
            pool=EASY_DAY_POOL,
            max_if=if_cap,
        ):
            continue
        if probe.workout is None or probe.tss <= d.tss + 1:
            continue
        before, before_tss = d.template_id, d.tss
        d.template_id, d.workout, d.params, d.outdoor = (
            probe.template_id,
            probe.workout,
            probe.params,
            probe.outdoor,
        )
        d.intent, d.role = probe.intent, probe.role
        habit = (
            f"你最近 4 週的輕鬆日中位數 {easy.raw:.0f} TSS（{easy.n} 天）"
            if easy.source == "learned" and easy.raw is not None
            else f"你在 athlete.yaml 設定的輕鬆日負荷 {easy.value:.0f} TSS"
        )
        d.notes.append(f"依{habit}排輕鬆日，強度上限 IF {if_cap:.2f}")
        out.append(
            Explanation(
                key=f"plan.learn.easy_day_tss.{d.date.isoformat()}",
                headline_zh=f"{d.date.isoformat()}：{before} → {d.template_id}（依你的輕鬆日習慣）",
                because=[
                    Reason(
                        text_zh=(
                            f"{habit}；收斂到模板預設後取 {easy.value:.0f} TSS，"
                            f"原本這天只排 {before_tss:.0f}"
                        ),
                        evidence={
                            "easy_day_tss": easy.value,
                            "n": easy.n,
                            "source": easy.source,
                            "median": easy.raw,
                            "before_tss": round(before_tss, 1),
                        },
                    ),
                    Reason(
                        text_zh=(
                            f"只排 60–90 分 Z2、強度上限 IF {if_cap:.2f}"
                            + ("（60 歲以上強度更保守）" if ctx.masters else "")
                            + "；準備度低於照表或本週已超前時不放寬"
                        ),
                        evidence={"if_cap": if_cap, "pool": list(EASY_DAY_POOL)},
                    ),
                ],
                method=MethodRef(
                    model_id="plan.learn.easy_day_tss",
                    version=ADAPT_VERSION,
                    inputs={},
                    doc="docs/specs/adaptive-planning.md",
                    claim="planning.easy_day_habit",
                ),
                confidence="medium",
                glossary_terms=["workout_library", "guardrails"],
            )
        )
    return out
