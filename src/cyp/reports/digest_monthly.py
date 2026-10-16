"""The monthly digest (``digest_monthly_v1``): the month's own sections, headline and assembly.

Here: consistency (2), the power-duration curve (4), FTP / CP / W' (5), the limiter shift (7)
and the research review summary (10). Load (3), TID by week (6), recovery (8) and the next month
(9) reuse the weekly builders over the month. A month without a ride is a break: period,
consistency (all skipped), load / CTL drift and next month only.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from pathlib import Path

from cyp.analysis import readiness as readiness_mod
from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal import pdc
from cyp.analysis.longitudinal.ftp import ROLLING_WINDOW
from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.ride.classify import CLASS_ZH
from cyp.core import evidence
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.planning.season import SEASON_VERSION
from cyp.reports import ride_common
from cyp.reports.digest_common import DigestInputs, days, join, list_join, method, text
from cyp.reports.digest_model import (
    Consistency,
    DigestHeadline,
    Distribution,
    FitnessModel,
    LimiterRank,
    LimiterShift,
    Load,
    MonthlyDigest,
    NextPlan,
    PowerCurve,
    Recovery,
    ResearchNote,
    SessionLine,
)
from cyp.reports.digest_progress import build_goal, build_next, build_recovery
from cyp.reports.digest_sections import (
    build_distribution,
    build_load,
    build_period,
    build_sessions,
    iso_weeks,
)
from cyp.reports.digest_weekly import (
    ON_TRACK_SHARE,
    PARTLY_SHARE,
    break_text,
    goal_clause,
    plan_share,
    tally,
)
from cyp.reports.ride_model import Grade, MethodUsed

#: Durations of the power curve: 5 s, 1, 5, 20 and 60 min (sprint, anaerobic, VO2, FTP, hour).
CURVE_DURATIONS_S: tuple[int, ...] = (5, 60, 300, 1200, 3600)
#: Near-maximal efforts listed under FTP / CP (sustained ones first, then short ones).
EFFORTS_MAX = 5
#: Before the season starts, "best" covers this many days back instead of the season.
BEST_FALLBACK_DAYS = 365
#: Monthly review reports (docs/specs/research-review.md).
REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCH_DIR = REPO_ROOT / "docs" / "research"
#: Planned sessions done below this share make consistency the month's one change.
CONSISTENCY_MIN = 0.7
#: Weeks over their hard-session target that make the distribution the one change.
OVER_WEEKS_ALERT = 2
#: REST verdicts in a month that make recovery the one change.
REST_DAYS_ALERT = 3
#: Limiters listed in the shift (the planner weighs all of them; the rest is noise here).
LIMITERS_MAX = 5
#: A row of the review's claim table: id, current grade, proposed grade, reason.
_CLAIM_ROW = re.compile(
    r"^\|\s*`([a-z0-9_.]+)`\s*\|\s*([ABC])[^|]*\|\s*\**([ABC])?[^|]*\|([^|]*)\|"
)


def month_bounds(month_start: dt.date) -> tuple[dt.date, dt.date]:
    """First and last day of ``month_start``'s calendar month."""
    first = month_start.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first, nxt - dt.timedelta(days=1)


# ------------------------------------------------------------------------------- consistency


def _longest_gap(inp: DigestInputs, start: dt.date, end: dt.date) -> int:
    best = run = 0
    for d in days(start, min(end, inp.today - dt.timedelta(days=1))):
        run = 0 if any(a.is_ride for a in inp.ds.on(d)) else run + 1
        best = max(best, run)
    return best


def build_consistency(
    inp: DigestInputs, start: dt.date, end: dt.date, sessions: Sequence[SessionLine]
) -> Consistency:
    """Planned sessions, those ridden and those achieved, per ISO week; the longest gap."""

    def counts(lines: Sequence[SessionLine]) -> tuple[int, int, int]:
        planned = [s for s in lines if s.planned_name is not None]
        done = [s for s in planned if s.verdict != "skipped"]
        return len(planned), len(done), sum(1 for s in done if s.verdict == "achieved")

    by_week = []
    for ws in iso_weeks(start, end):
        we = ws + dt.timedelta(days=6)
        by_week.append((ws, *counts([s for s in sessions if ws <= s.day <= we])))
    planned, done, achieved = counts(sessions)
    gap = _longest_gap(inp, start, end)
    return Consistency(
        planned=planned,
        done=done,
        achieved=achieved,
        by_week=by_week,
        longest_gap_days=gap,
        text_zh=text("consistency.text", planned=planned, done=done, achieved=achieved, gap=gap),
    )


# ------------------------------------------------------------------------------- power curve


def _best(inp: DigestInputs, start: dt.date, end: dt.date) -> list[float | None]:
    mmp = pdc.mmp_from_rides(
        {str(k): v for k, v in r.power_curve.items()}
        for r in inp.ds.rides(start, end)
        if r.power_ok
    )
    return [round(mmp[d]) if d in mmp else None for d in CURVE_DURATIONS_S]


def build_power_curve(inp: DigestInputs, start: dt.date, end: dt.date) -> PowerCurve | None:
    """Best power per duration: this month, last month, and the season to date.

    The season window starts at the season start (or the month's first day when the season
    starts inside the month); before the season (or without a config) "best" covers the last
    :data:`BEST_FALLBACK_DAYS` days instead.
    """
    this = _best(inp, start, end)
    if all(w is None for w in this):
        return None
    last_start, last_end = month_bounds(start - dt.timedelta(days=1))
    in_season = inp.cfg is not None and inp.cfg.season.start <= end
    since = (
        min(inp.cfg.season.start, start)
        if inp.cfg is not None and in_season
        else end - dt.timedelta(days=BEST_FALLBACK_DAYS - 1)
    )
    best_key = "curve.season_best" if in_season else "curve.year_best"
    season = _best(inp, since, end)
    last = _best(inp, last_start, last_end)
    parts = []
    for d, w, w0, wb in zip(CURVE_DURATIONS_S, this, last, season, strict=True):
        if w is None:
            continue
        name = text(f"curve.d{d}")
        clause = (
            text("curve.vs_last", name=name, w=w, last=w0)
            if w0 is not None
            else text("curve.no_last", name=name, w=w)
        )
        if wb is not None and w >= wb:
            clause += text(best_key, days=BEST_FALLBACK_DAYS)
        parts.append(clause)
    return PowerCurve(
        durations_s=list(CURVE_DURATIONS_S),
        this_month_w=this,
        last_month_w=last,
        season_best_w=season,
        best_since=since,
        text_zh=join(parts),
    )


# ------------------------------------------------------------------------------- FTP / CP


def build_fitness(inp: DigestInputs, start: dt.date, end: dt.date) -> FitnessModel | None:
    """FTP setting at both ends, the CP / W' fit of the last 42 days, the efforts behind them."""
    tr = inp.trends_at(end)
    ftp0, ftp1 = inp.ds.ftp_on(start), inp.ds.ftp_on(end)
    if tr is None and ftp1 is None:
        return None
    fit = tr.ftp.windows["42d"].cp_2p if tr is not None and "42d" in tr.ftp.windows else None
    efforts: list[str] = []
    if tr is not None:
        seen: set[tuple[str, str]] = set()
        for item in [*tr.ftp.max_efforts.sustained, *tr.ftp.max_efforts.short]:
            key = (str(item.get("date")), str(item.get("label_zh")))
            if key in seen or not start.isoformat() <= key[0] <= end.isoformat():
                continue
            seen.add(key)
            efforts.append(text("fitness.effort", day=key[0], label=key[1]))
    n_efforts = len(efforts)
    efforts = efforts[:EFFORTS_MAX]
    reasons = []
    if ftp0 is not None and ftp1 is not None:
        reasons.append(
            Reason(
                text_zh=text("fitness.setting", start=ftp0, end=ftp1, diff=ftp1 - ftp0),
                evidence={"ftp_start_w": ftp0, "ftp_end_w": ftp1},
            )
        )
    if fit is not None:
        reasons.append(
            Reason(
                text_zh=text(
                    "fitness.cp",
                    cp=fit.cp,
                    wp=fit.w_prime / 1000,
                    days=ROLLING_WINDOW,
                    n=len(fit.points),
                    eftp=fit.eftp,
                    frac=pdc.CP_TO_FTP,
                ),
                evidence={
                    "cp_w": round(fit.cp, 1),
                    "w_prime_j": round(fit.w_prime),
                    "points": [[d, round(w, 1)] for d, w in fit.points],
                    "r2": round(fit.r2, 4) if fit.r2 is not None else None,
                },
            )
        )
    else:
        reasons.append(Reason(text_zh=text("fitness.no_cp", days=ROLLING_WINDOW)))
    reasons.append(
        Reason(
            text_zh=text("fitness.efforts", n=n_efforts, shown=len(efforts))
            if efforts
            else text("fitness.no_efforts"),
            evidence={"n_efforts": n_efforts},
        )
    )
    goal = build_goal(inp, start, end)
    if goal is not None:
        reasons.append(Reason(text_zh=goal.text_zh))
    expl = Explanation(
        key=f"digest.fitness.{start.isoformat()}",
        headline_zh=reasons[0].text_zh,
        because=reasons,
        method=MethodRef(
            model_id="cp_2p",
            version=pdc.PDC_VERSION,
            inputs={"window_days": ROLLING_WINDOW, "cp_to_ftp": pdc.CP_TO_FTP},
            doc="docs/glossary/cp_wprime.md",
            claim="cp_wprime.cp_boundary",
        ),
        confidence="medium" if fit is not None and efforts else "low",
        glossary_terms=["cp_wprime", "eftp", "ftp_target_season"],
    )
    return FitnessModel(
        ftp_start_w=ftp0,
        ftp_end_w=ftp1,
        cp_w=round(fit.cp, 1) if fit is not None else None,
        w_prime_j=float(round(fit.w_prime)) if fit is not None else None,
        backing_efforts_zh=efforts,
        goal=goal,
        text_zh=join(r.text_zh for r in reasons[:3]),
        explanation=expl.to_json_dict(),
    )


# ------------------------------------------------------------------------------- limiters


def build_limiters(inp: DigestInputs, start: dt.date, end: dt.date) -> LimiterShift | None:
    """Limiter ranks at the month end vs the day before the month, and the planner's bias."""
    now, before = inp.trends_at(end), inp.trends_at(start - dt.timedelta(days=1))
    if now is None:
        return None
    names: dict[str, str] = ride_common.texts()["limiter_names"]
    rank_now = {str(lim["id"]): i for i, lim in enumerate(now.report.limiters, start=1)}
    rank_before = (
        {str(lim["id"]): i for i, lim in enumerate(before.report.limiters, start=1)}
        if before is not None
        else {}
    )
    ids = sorted(set(rank_now) | set(rank_before), key=lambda i: (rank_now.get(i, 99), i))
    lines = [
        LimiterRank(
            limiter_id=i,
            name_zh=names.get(i, i),
            rank_now=rank_now.get(i),
            rank_before=rank_before.get(i),
        )
        for i in ids[:LIMITERS_MAX]
    ]
    up = {k: v for k, v in now.report.planner_bias.items() if v > 1}
    if not rank_now:
        gone = [ln.name_zh for ln in lines if ln.rank_before is not None]
        response = (
            text("limiters.resolved", names=list_join(gone)) if gone else text("limiters.none")
        )
    elif up:
        listed = list_join(
            text("limiters.bias", cls=CLASS_ZH.get(k, k), b=v)
            for k, v in sorted(up.items(), key=lambda kv: -kv[1])
        )
        response = text("limiters.response", name=lines[0].name_zh, bias=listed)
    else:
        response = text("limiters.no_bias", name=lines[0].name_zh)
    return LimiterShift(lines=lines, plan_response_zh=response)


# ------------------------------------------------------------------------------- research


def build_research(start: dt.date) -> ResearchNote | None:
    """The review report of this month, with the grade changes the registry now carries.

    A change counts as approved when the registry's grade differs from the report's
    "current" column (the approval commit edits ``core/evidence.py``).
    """
    path = RESEARCH_DIR / f"{start:%Y-%m}.md"
    if not path.is_file():
        return None
    changes: list[tuple[str, Grade, Grade]] = []
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _CLAIM_ROW.match(line)
        if m is None or m.group(1) not in evidence.CLAIMS:
            continue
        claim_id, before = m.group(1), m.group(2)
        after = evidence.grade(claim_id)
        if after != before:
            changes.append((claim_id, before, after))  # type: ignore[arg-type]
            lines.append(
                text(
                    "research.change",
                    claim=claim_id,
                    before=before,
                    after=after,
                    why=m.group(4).strip(),
                )
            )
    return ResearchNote(
        report_path=path.relative_to(REPO_ROOT).as_posix(),
        approved_changes_zh=lines or [text("research.none")],
        grade_changes=changes,
    )


# ------------------------------------------------------------------------------- headline


def build_headline(
    consistency: Consistency,
    load: Load | None,
    weeks: Sequence[tuple[dt.date, Distribution]],
    limiters: LimiterShift | None,
    recovery: Recovery | None,
    fitness: FitnessModel | None,
    next_month: NextPlan | None,
    sessions: Sequence[SessionLine],
    *,
    is_break: bool,
    break_reason: str,
) -> DigestHeadline:
    """Section 1, deterministic: consistency, CTL vs the plan path, the goal, one change."""
    drift = (
        text("headline.ctl_drift", start=load.ctl_start, end=load.ctl_end)
        if load is not None and load.ctl_start is not None and load.ctl_end is not None
        else ""
    )
    if is_break:
        first = next_month.sessions[0] if next_month and next_month.sessions else None
        return DigestHeadline(
            text_zh=break_text(break_reason, consistency.planned) + drift,
            verdict="break",
            change_zh=text("change.break_return", day=first.day, name=first.name) if first else "",
        )
    _, share = plan_share(tally(sessions))
    parts = []
    if share is None:
        verdict = "no_data"
        parts.append(text("headline.no_plan", rides=load.rides if load else 0))
    else:
        verdict = (
            "on_track"
            if share >= ON_TRACK_SHARE
            else "partly"
            if share >= PARTLY_SHARE
            else "off_track"
        )
        parts.append(
            text(
                "headline.month",
                planned=consistency.planned,
                done=consistency.done,
                achieved=consistency.achieved,
            )
        )
    if load is not None and load.ctl_start is not None and load.ctl_end is not None:
        clause = text("headline.month_ctl", start=load.ctl_start, end=load.ctl_end)
        if load.ctl_plan:
            clause += text("headline.month_plan", plan=load.ctl_plan[-1][1])
        parts.append(clause)
    parts.append(goal_clause(fitness.goal if fitness else None))
    over = sum(1 for _, d in weeks if d.verdict == "over")
    done_share = consistency.done / consistency.planned if consistency.planned else None
    if done_share is not None and done_share < CONSISTENCY_MIN:
        change = text(
            "change.consistency",
            done=consistency.done,
            planned=consistency.planned,
            gap=consistency.longest_gap_days,
        )
    elif over >= OVER_WEEKS_ALERT:
        change = text("change.too_hard_month", n=over)
    elif recovery is not None and recovery.readiness_counts.get("REST", 0) >= REST_DAYS_ALERT:
        change = text(
            "change.recovery_month",
            rest=recovery.readiness_counts["REST"],
            low=sum(
                1
                for d in recovery.days
                if d.hrv_z7 is not None and d.hrv_z7 < -readiness_mod.HRV_SWC_SD
            ),
        )
    elif limiters is not None and limiters.lines and limiters.lines[0].rank_now == 1:
        change = text("change.limiter", name=limiters.lines[0].name_zh)
    else:
        change = text("change.keep")
    return DigestHeadline(
        text_zh=join(parts) + text("headline.end"),
        verdict=verdict,  # type: ignore[arg-type]
        change_zh=change,
    )


# ------------------------------------------------------------------------------- assembly


def _methods(d: MonthlyDigest, sessions: Sequence[SessionLine]) -> list[MethodUsed]:
    ids: list[str] = []
    if any(s.activity_id for s in sessions):
        ids.append("coggan_np_if_tss")
    if d.consistency is not None and d.consistency.done:
        ids.append(intent_mod.INTENT_VERSION)
    if d.load is not None:
        ids.append("banister_pmc")
        if d.load.ctl_plan:
            ids.append(SEASON_VERSION)
    if d.power_curve is not None:
        ids.append("power_curve")
    if d.fitness is not None:
        ids.append("cp_2p")
        if d.fitness.goal is not None:
            ids += [goal_mod.GOAL_VERSION, "eftp"]
    if d.distribution_by_week:
        ids.append("tid_3zone")
    if d.limiters is not None:
        ids.append("limiters")
    if d.recovery is not None:
        ids.append(readiness_mod.ALGO_VERSION)
    if d.next_month is not None and d.next_month.sessions:
        ids.append("planner")
    if d.research is not None:
        ids.append("research_review")
    return [method(i) for i in ids]


def build(inp: DigestInputs, month_start: dt.date, *, generated_at: dt.datetime) -> MonthlyDigest:
    """Every section of one calendar month."""
    start, end = month_bounds(month_start)
    period = build_period(inp, start, end, text("period.month", d=start))
    sessions = build_sessions(inp, start, end)
    consistency = build_consistency(inp, start, end, sessions)
    load = build_load(inp, start, end, sessions, with_plan_path=True)
    nxt_start, nxt_end = month_bounds(end + dt.timedelta(days=1))
    next_month = build_next(inp, nxt_start, nxt_end, monthly=True)
    research = build_research(start)
    if period.is_break:
        curve = fitness = limiters = recovery = None
        weeks: list[tuple[dt.date, Distribution]] = []
    else:
        curve = build_power_curve(inp, start, end)
        fitness = build_fitness(inp, start, end)
        limiters = build_limiters(inp, start, end)
        recovery = build_recovery(inp, start, end)
        weeks = [(ws, x) for ws in iso_weeks(start, end) if (x := build_distribution(inp, ws))]
    d = MonthlyDigest(
        profile=inp.profile,
        athlete_name=inp.athlete_name,
        generated_at=generated_at,
        period=period,
        headline=build_headline(
            consistency,
            load,
            weeks,
            limiters,
            recovery,
            fitness,
            next_month,
            sessions,
            is_break=period.is_break,
            break_reason=period.break_reason_zh,
        ),
        consistency=consistency,
        load=load,
        power_curve=curve,
        fitness=fitness,
        distribution_by_week=weeks,
        limiters=limiters,
        recovery=recovery,
        next_month=next_month,
        research=research,
    )
    d.methods = _methods(d, sessions)
    return d
