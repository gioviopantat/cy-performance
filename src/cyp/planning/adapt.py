"""Daily adaptation of the horizon (docs/05 §3 steps 1, 2 and 4).

Applied to freshly planned days before the guardrails:

- **Calendar collisions**: athlete-created events win. ``HOLIDAY`` / ``SICK`` / ``INJURED``
  -> no workout; a ``RACE_*`` or the athlete's own ``WORKOUT`` on a day -> we leave that day
  to them. (Limited-availability NOTEs are applied earlier as minute overrides.)
- **Yesterday**: a missed hard session is re-slotted within 72 h onto an endurance day that
  keeps 48 h from other hard days, else dropped (never stacked); a ride > 130 % of its plan
  turns today into recovery.
- **Readiness today**: ``REST`` -> rest (a hard session moves ≥ 48 h later if possible);
  ``EASY`` -> at most Z2 at 60 % of the planned TSS; ``UPGRADE`` -> one progression step more on
  a hard day if TSB > −10 and yesterday was not hard (never adds a new hard session);
  ``AS_PLANNED`` -> unchanged.

Every change appends an Explanation to the returned list (key ``plan.adapt.<what>.<date>``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.planner import DayPlan, PlanContext, fill_endurance, pick_params

ADAPT_VERSION = "adapt_v1"
BLOCKING_CATEGORIES = frozenset({"HOLIDAY", "SICK", "INJURED"})
ATHLETE_OWNS = frozenset({"RACE_A", "RACE_B", "RACE_C", "WORKOUT"})
EASY_FACTOR = 0.6
OVERDONE_FACTOR = 1.3
RESLOT_WINDOW_DAYS = 3


@dataclass(frozen=True)
class Yesterday:
    """What happened to yesterday's planned session."""

    planned: DayPlan | None
    actual_load: float | None
    completed_hard: bool  # an analysed ride classified hard happened yesterday


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
    if p.is_hard and not y.completed_hard and (y.actual_load or 0) < 0.5 * p.tss:
        out += reslot_missed_hard(days, p, today, ctx, phase, progress)
    if y.actual_load is not None and p.tss > 0 and y.actual_load > OVERDONE_FACTOR * p.tss:
        t = next((d for d in days if d.date == today), None)
        if t is not None and t.workout is not None:
            before = t.template_id
            fill_endurance(t, 20.0, ctx, phase, prefer_cadence=False)
            t.notes.append("昨天負荷超過計畫 130 %，今天改恢復")
            out.append(
                _expl(
                    "overdone",
                    today,
                    f"{today.isoformat()}：{before} → {t.template_id}",
                    [
                        Reason(
                            text_zh=f"昨天實際 {y.actual_load:.0f} TSS，計畫 {p.tss:.0f}",
                            evidence={"actual": y.actual_load, "planned": round(p.tss)},
                        )
                    ],
                )
            )
    return out


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
