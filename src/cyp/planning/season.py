"""Season skeleton for an ``ftp_target`` season: blocks, 3:1 weeks, tests, weekly load targets.

Pure functions over :class:`~cyp.settings.AthleteConfig` (docs/05 §2.1, §2.4; glossary
``ftp_target_season`` and ``periodization_3_1``):

- Weeks are Monday-based; week 1 starts on ``season.start``; the season runs through the week
  containing the goal date (2026-10-05 -> 2027-04-02 = 26 weeks).
- The last :data:`TEST_WEEKS` weeks are the test week + consolidation. The rest is split, from
  the back, into a threshold/VO2 block and a build block of :data:`BLOCK_WEEKS` each; base
  gets what is left (shorter seasons shrink base first, then build; base keeps ≥ 4 weeks).
- 3:1 load pattern inside each block: every 4th week is a recovery week (TSS × 0.6, one HIT
  fewer). With ``2:1`` every 3rd week.
- Tests from ``season.tests`` (``every_weeks`` / ``offset_week``) plus a final 20-min test in
  the last week. They fall on recovery weeks in the default config.
- Weekly target TSS (:func:`week_targets`): loading weeks use the PMC closed form for the
  block's CTL ramp target (``load_for_ctl_target`` over 7 days), capped by the weekly hour
  ceiling at the phase's typical IF; recovery weeks are 60 % of the preceding loading week.
- Checkpoint miss > 3 % (:func:`apply_checkpoint`) makes the next block repeat its first two
  weeks' progression instead of advancing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from cyp.analysis.longitudinal.pmc import PMCState, load_for_ctl_target, simulate
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.timeutil import week_start
from cyp.settings import AthleteConfig

SEASON_VERSION = "season_v1"
PlanPhase = Literal["base", "build", "threshold", "test"]
TestKind = Literal["ramp", "twenty_min"]

TEST_WEEKS = 2
BLOCK_WEEKS = 8
MIN_BASE_WEEKS = 4
RECOVERY_FACTOR = 0.6
TEST_WEEK_FACTOR = 0.6
CHECKPOINT_MISS = 0.03
#: CTL ramp target per week (docs/05 §2.1: base +3–5, build +4–6; threshold holds/gains a little).
RAMP_TARGET: dict[str, float] = {"base": 4.0, "build": 4.0, "threshold": 2.0, "test": -3.0}
#: Typical weekly IF used to convert hours <-> TSS for the hour ceiling.
PHASE_IF: dict[str, float] = {"base": 0.68, "build": 0.71, "threshold": 0.72, "test": 0.66}
DEFAULT_HIT: dict[str, int] = {"base": 1, "build": 2, "threshold": 2, "test": 1}
PHASE_ZH: dict[str, str] = {
    "base": "基礎期",
    "build": "建構期",
    "threshold": "閾值／VO2 期",
    "test": "測驗週",
}


@dataclass(frozen=True)
class SeasonWeek:
    """One week of the skeleton."""

    index: int  # 1-based season week
    start: dt.date
    phase: PlanPhase
    block_idx: int  # 0-based
    week_in_block: int  # 1-based
    recovery: bool
    test: TestKind | None
    hit_sessions: int
    ramp_target: float
    progression_index: int  # week_in_block counted over loading weeks, after checkpoint repeats

    @property
    def end(self) -> dt.date:
        """Sunday of the week."""
        return self.start + dt.timedelta(days=6)


@dataclass
class SeasonSkeleton:
    """All weeks of the season, plus the facts used to build them."""

    start: dt.date
    goal_date: dt.date
    weeks: list[SeasonWeek] = field(default_factory=list)
    load_pattern: str = "3:1"

    def week_of(self, day: dt.date) -> SeasonWeek | None:
        """The season week containing ``day`` (``None`` outside the season)."""
        for w in self.weeks:
            if w.start <= day <= w.end:
                return w
        return None

    def block_weeks(self, block_idx: int) -> list[SeasonWeek]:
        """Weeks of one block in order."""
        return [w for w in self.weeks if w.block_idx == block_idx]


def _block_lengths(total: int) -> list[tuple[PlanPhase, int]]:
    body = total - TEST_WEEKS
    if body < MIN_BASE_WEEKS:
        raise ValueError(f"season too short: {total} weeks")
    threshold = min(BLOCK_WEEKS, max(body - MIN_BASE_WEEKS, 0))
    build = min(BLOCK_WEEKS, max(body - MIN_BASE_WEEKS - threshold, 0))
    base = body - build - threshold
    out: list[tuple[PlanPhase, int]] = [("base", base)]
    if build:
        out.append(("build", build))
    if threshold:
        out.append(("threshold", threshold))
    out.append(("test", TEST_WEEKS))
    return out


def _tests_for(cfg: AthleteConfig, week: int, total: int) -> TestKind | None:
    t = cfg.season.tests
    if (
        t.twenty_min
        and week >= t.twenty_min.offset_week
        and ((week - t.twenty_min.offset_week) % t.twenty_min.every_weeks == 0)
    ):
        return "twenty_min"
    if (
        t.ramp
        and week >= t.ramp.offset_week
        and ((week - t.ramp.offset_week) % t.ramp.every_weeks == 0)
    ):
        return "ramp"
    if week == total:
        return "twenty_min"
    return None


def build_skeleton(
    cfg: AthleteConfig, *, repeat_blocks: frozenset[int] = frozenset()
) -> SeasonSkeleton:
    """Season weeks from the athlete config.

    ``repeat_blocks``: block indices whose first two weeks' progression must be repeated
    (a missed checkpoint at the end of the previous block, see :func:`apply_checkpoint`).

    Raises:
        ValueError: no goal / goal before the season / season shorter than base + test.
    """
    goal = next((g for g in cfg.goals if g.kind == "ftp_target"), None) or (
        cfg.goals[0] if cfg.goals else None
    )
    if goal is None:
        raise ValueError("athlete config has no goal")
    start = week_start(cfg.season.start)
    total = (week_start(goal.date) - start).days // 7 + 1
    cycle = 4 if cfg.season.load_pattern == "3:1" else 3
    sk = SeasonSkeleton(start=start, goal_date=goal.date, load_pattern=cfg.season.load_pattern)
    idx = 0
    for block_idx, (phase, n) in enumerate(_block_lengths(total)):
        loading_seen = 0
        hit = cfg.planner.hit_per_week.get(phase, DEFAULT_HIT[phase])
        ramp = min(RAMP_TARGET[phase], cfg.planner.ramp_cap.get(phase, RAMP_TARGET[phase]))
        for k in range(1, n + 1):
            idx += 1
            recovery = phase != "test" and k % cycle == 0
            if not recovery:
                loading_seen += 1
            prog = loading_seen if not recovery else max(loading_seen, 1)
            if block_idx in repeat_blocks and prog > 2:
                prog -= 2
            sk.weeks.append(
                SeasonWeek(
                    index=idx,
                    start=start + dt.timedelta(weeks=idx - 1),
                    phase=phase,
                    block_idx=block_idx,
                    week_in_block=k,
                    recovery=recovery,
                    test=_tests_for(cfg, idx, total),
                    hit_sessions=max(hit - (1 if recovery else 0), 0),
                    ramp_target=ramp if not recovery else 0.0,
                    progression_index=prog,
                )
            )
    return sk


@dataclass(frozen=True)
class WeekTargets:
    """Load targets for one week given the CTL it starts from."""

    week: SeasonWeek
    ctl_start: float
    target_tss: float
    target_hours: float
    ctl_end: float
    capped_by_hours: bool
    explanation: Explanation


def week_targets(
    week: SeasonWeek,
    ctl_start: float,
    *,
    weekly_max_minutes: int,
    prev_loading_tss: float | None = None,
    atl_start: float | None = None,
) -> WeekTargets:
    """Target TSS / hours for ``week`` starting at ``ctl_start`` (see module docstring)."""
    if_ = PHASE_IF[week.phase]
    cap_tss = weekly_max_minutes / 60.0 * if_**2 * 100.0
    seed = PMCState(week.start - dt.timedelta(days=1), ctl_start, atl_start or ctl_start)
    reasons: list[Reason] = []
    if week.recovery or week.phase == "test":
        factor = RECOVERY_FACTOR if week.recovery else TEST_WEEK_FACTOR
        base = prev_loading_tss if prev_loading_tss else 7 * ctl_start
        tss = base * factor
        reasons.append(
            Reason(
                text_zh=(
                    f"{'恢復週' if week.recovery else '測驗週'}："
                    f"前一個加壓週 {base:.0f} TSS × {factor:.0%}"
                ),
                evidence={"base_tss": round(base), "factor": factor},
            )
        )
    else:
        daily = load_for_ctl_target(seed, ctl_start + week.ramp_target, 7)
        tss = daily * 7
        reasons.append(
            Reason(
                text_zh=(
                    f"CTL {ctl_start:.1f} 每週目標 +{week.ramp_target:g}，"
                    f"依 PMC 閉式解每天約 {daily:.0f} TSS → 一週 {tss:.0f}"
                ),
                evidence={
                    "ctl_start": round(ctl_start, 2),
                    "ramp_target": week.ramp_target,
                    "daily_tss": round(daily, 1),
                    "week_tss": round(tss),
                },
            )
        )
    capped = tss > cap_tss
    if capped:
        reasons.append(
            Reason(
                text_zh=(
                    f"超過每週 {weekly_max_minutes / 60:.0f} 小時上限（IF {if_:.2f} 約 "
                    f"{cap_tss:.0f} TSS），以上限為準"
                ),
                evidence={"cap_tss": round(cap_tss), "weekly_max_minutes": weekly_max_minutes},
            )
        )
        tss = cap_tss
    hours = tss / (if_**2 * 100.0)
    end = simulate(seed, [tss / 7.0] * 7)[-1]
    tag = "（恢復週）" if week.recovery else ""
    expl = Explanation(
        key=f"plan.week.{week.start.isoformat()}",
        headline_zh=(
            f"第 {week.index} 週 {PHASE_ZH[week.phase]}{tag}：目標 {tss:.0f} TSS、"
            f"約 {hours:.1f} 小時，CTL {ctl_start:.0f} → {end.ctl:.0f}"
        ),
        because=reasons,
        method=MethodRef(
            model_id="periodization_3_1",
            version=SEASON_VERSION,
            inputs={"phase": week.phase, "if": if_, "recovery": week.recovery},
            doc="docs/glossary/periodization_3_1.md",
        ),
        confidence="medium",
        glossary_terms=["periodization_3_1", "banister_pmc", "ftp_target_season"],
    )
    return WeekTargets(
        week, ctl_start, round(tss, 1), round(hours, 2), round(end.ctl, 2), capped, expl
    )


def season_projection(
    sk: SeasonSkeleton, ctl_start: float, *, weekly_max_minutes: int
) -> list[WeekTargets]:
    """Targets for every week assuming each is completed as planned (season overview)."""
    out: list[WeekTargets] = []
    ctl = ctl_start
    last_loading: float | None = None
    for w in sk.weeks:
        t = week_targets(
            w, ctl, weekly_max_minutes=weekly_max_minutes, prev_loading_tss=last_loading
        )
        out.append(t)
        ctl = t.ctl_end
        if not w.recovery and w.phase != "test":
            last_loading = t.target_tss
    return out


def checkpoint_missed(expected_ftp: float, measured_ftp: float) -> bool:
    """True when the measured FTP is more than :data:`CHECKPOINT_MISS` below expectation."""
    return measured_ftp < expected_ftp * (1 - CHECKPOINT_MISS)


def apply_checkpoint(
    cfg: AthleteConfig, measured: Mapping[int, float], repeat: frozenset[int] = frozenset()
) -> frozenset[int]:
    """Blocks to repeat given measured FTP per checkpoint week (``{week: ftp}``).

    A miss at the checkpoint closing block *b* (its last week) repeats block *b + 1*'s first two
    weeks. Unmeasured checkpoints change nothing.
    """
    sk = build_skeleton(cfg, repeat_blocks=repeat)
    goal = next((g for g in cfg.goals if g.kind == "ftp_target"), None)
    out = set(repeat)
    for cp in goal.checkpoints if goal else []:
        got = measured.get(cp.week)
        if got is None or not checkpoint_missed(cp.ftp, got):
            continue
        week = next((w for w in sk.weeks if w.index == cp.week), None)
        if week is not None:
            out.add(week.block_idx + 1)
    return frozenset(out)


def with_progression(week: SeasonWeek, delta: int) -> SeasonWeek:
    """Copy of ``week`` with its progression index moved by ``delta`` (≥ 1)."""
    return replace(week, progression_index=max(week.progression_index + delta, 1))
