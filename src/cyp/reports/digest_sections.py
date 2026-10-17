"""Digest sections shared by the weekly and monthly digests: period, sessions, load, TID.

- period: the dates covered, the season week / phase, and whether it was a break (no ride),
  with the reason when the athlete's availability or calendar gives one.
- sessions (weekly 2, the base of monthly 2): every planned session and every ride, judged by
  the per-ride intent check (:func:`~cyp.reports.ride_intent.build_intent`), outdoor stops and
  heat named from :func:`~cyp.analysis.ride.context.outdoor_context`.
- load (weekly 3 / monthly 3): planned vs actual TSS per day, CTL / ATL / TSB from
  ``fitness_daily``, the ramp vs the phase's cap and, monthly, the season plan's CTL path.
- distribution (weekly 4 / monthly 6): sessions per intensity zone from the TID model
  (``tid_v2``, ADR-0014) vs the phase target.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from cyp.analysis.longitudinal import tid as tid_mod
from cyp.analysis.ride import context as ctx_mod
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.timeutil import week_start
from cyp.planning.adapt import OVERDONE_FACTOR, OVERDONE_MARGIN_TSS, is_overdone
from cyp.planning.season import PHASE_ZH, season_projection
from cyp.planning.templates import CEILING_INTENTS
from cyp.reports.digest_common import DigestInputs, days, join, list_join, text
from cyp.reports.digest_model import Band, Distribution, Load, Period, SessionLine, SessionVerdict
from cyp.reports.facts import DEFAULT_RAMP_CAP
from cyp.reports.ride_common import RideInputs
from cyp.reports.ride_intent import build_intent, planned_for
from cyp.reports.ride_model import Intent
from cyp.reports.ride_sections import usual_temp
from cyp.store.models import IcuEvent, PlannedWorkout
from cyp.store.repo.events import CYP_EXTERNAL_ID_PREFIX
from cyp.store.repo.planned import is_live

#: Hard sessions (mid + high) a week may be off its phase target before the distribution is
#: called under / over: one session either way, since a week holds only 4-7 sessions and one
#: session is already 15-25 percentage points.
TID_SESSION_SLACK = 1
BANDS: tuple[Band, ...] = ("low", "mid", "high")


# ------------------------------------------------------------------------------- period


def unavailable_days(inp: DigestInputs, start: dt.date, end: dt.date) -> list[dt.date]:
    """Days in the period the athlete set to 0 available minutes (``availability.dates``)."""
    if inp.cfg is None:
        return []
    return sorted(d for d, m in inp.cfg.availability.dates.items() if m == 0 and start <= d <= end)


def _ranges(ds: Sequence[dt.date]) -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    for d in ds:
        if out and d == out[-1][1] + dt.timedelta(days=1):
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


def _range_text(lo: dt.date, hi: dt.date) -> str:
    if lo == hi:
        return text("period.day", d=lo)
    return text("period.range", lo=lo, hi=hi)


def break_reason(inp: DigestInputs, start: dt.date, end: dt.date) -> str:
    """Why a period has no rides: availability 0 dates, a sick / injured event, or unknown."""
    off = unavailable_days(inp, start, end)
    parts = []
    if off:
        listed = list_join(_range_text(lo, hi) for lo, hi in _ranges(off))
        parts.append(text("period.break_unavailable", ranges=listed, n=len(off)))
    sick = [
        e
        for e in inp.ds.events
        if e.category in ("SICK", "INJURED")
        and e.date is not None
        and e.date <= end
        and (e.end_date or e.date) >= start
    ]
    for e in sick:
        parts.append(
            text(
                f"period.break_{e.category.lower()}",
                range=_range_text(max(e.date or start, start), min(e.end_date or end, end)),
            )
        )
    return join(parts) if parts else text("period.break_unknown")


def build_period(inp: DigestInputs, start: dt.date, end: dt.date, label_zh: str) -> Period:
    """The period, its season week / phase, and the break flag (no ride at all)."""
    week = inp.week_of(start) or inp.week_of(end)
    is_break = not inp.rides(start, end)
    return Period(
        start=start,
        end=end,
        label_zh=label_zh,
        season_week=week.index if week is not None else None,
        phase=week.phase if week is not None else None,
        is_break=is_break,
        break_reason_zh=break_reason(inp, start, end) if is_break else "",
    )


def week_label(inp: DigestInputs, start: dt.date) -> str:
    """Dates, ISO week, and the season week / phase (``period.*`` texts)."""
    end = start + dt.timedelta(days=6)
    label = text("period.week", start=start, end=end, iso=start.isocalendar().week)
    w = inp.week_of(start)
    if w is not None:
        label += text("period.season_week", n=w.index, phase=PHASE_ZH[w.phase])
        if w.recovery:
            label += text("period.recovery")
    return label


# ------------------------------------------------------------------------------- sessions


def day_plans(inp: DigestInputs, day: dt.date) -> tuple[list[PlannedWorkout], list[IcuEvent]]:
    """The day's live planned workouts of ours, else the athlete's own icu WORKOUT events.

    Same precedence as :func:`~cyp.reports.ride_intent.planned_for`.
    """
    ours = list(
        inp.session.scalars(
            select(PlannedWorkout)
            .where(
                PlannedWorkout.athlete_id == inp.ds.athlete_id,
                PlannedWorkout.date_local == day,
                is_live(),
            )
            .order_by(PlannedWorkout.slot)
        )
    )
    if ours:
        return ours, []
    events = inp.session.scalars(
        select(IcuEvent)
        .where(
            IcuEvent.category == "WORKOUT",
            IcuEvent.start_date_local >= day.isoformat(),
            IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
        )
        .order_by(IcuEvent.id)
    )
    return [], [e for e in events if not (e.external_id or "").startswith(CYP_EXTERNAL_ID_PREFIX)]


def _is_easy(intent: Intent, pw: PlannedWorkout | None) -> bool:
    return intent.kind == "ceiling" or (pw is not None and pw.intent in CEILING_INTENTS)


def session_verdict(
    intent: Intent | None, pw: PlannedWorkout | None, *, casual: bool = False
) -> SessionVerdict:
    """The ride's intent verdict; ``overdone`` for an easy day past the planner's factor.

    A casual ride is ``casual`` whatever its load (never overdone, never missed).
    """
    if casual:
        return "casual"
    if intent is None:
        return "unplanned"
    if _is_easy(intent, pw) and is_overdone(intent.load_actual, intent.load_planned):
        return "overdone"
    v = intent.verdict
    return "no_data" if v == "unplanned" else v


def _outdoor_notes(r: RideInputs) -> list[str]:
    frame = r.frame
    if r.indoor or frame is None:
        return []
    f = ctx_mod.outdoor_context(frame, usual_temp(r))
    codes = {c.code for c in f.caveats}
    out = []
    if "stops" in codes:
        out.append(text("session.stops", n=len(f.stops), minutes=round(f.stopped_s / 60)))
    if codes & {"heat", "heat_vs_usual"} and f.temp_avg_c is not None:
        out.append(text("session.heat", temp=round(f.temp_avg_c)))
    return out


def _ride_line(r: RideInputs) -> tuple[SessionLine, tuple[str, int] | None]:
    pw, ev = planned_for(r)
    intent = build_intent(r) if pw is not None or ev is not None else None
    verdict = session_verdict(intent, pw, casual=r.casual)
    fact = r.fact
    notes: list[str] = []
    if intent is None:
        msg = "session.casual" if r.casual else "session.unplanned"
        notes.append(text(msg, cls=fact["classification_zh"] or fact["sport"]))
    elif verdict == "overdone":
        notes.append(
            text(
                "session.overdone",
                ratio=intent.load_ratio,
                max=OVERDONE_FACTOR,
                margin=OVERDONE_MARGIN_TSS,
            )
        )
    else:
        notes.append(str((intent.explanation or {}).get("headline_zh", "")))
    if intent is not None and intent.excused:
        notes.append(text("session.excused", n=len(intent.excused)))
    notes += _outdoor_notes(r)
    load = fact["load"]
    line = SessionLine(
        day=r.day,
        planned_name=intent.planned_name if intent is not None else None,
        planned_tss=intent.load_planned if intent is not None else None,
        ride_name=fact["name"],
        activity_id=r.activity.id,
        actual_tss=float(round(load)) if load is not None else None,
        moving_min=fact["moving_min"],
        outdoor=not r.indoor,
        verdict=verdict,
        note_zh=join(notes),
    )
    key = ("pw", pw.id) if pw is not None else ("ev", ev.id) if ev is not None else None
    return line, key


def build_sessions(inp: DigestInputs, start: dt.date, end: dt.date) -> list[SessionLine]:
    """Every ride and every planned session of the period, day by day.

    A planned session with no ride is ``skipped`` once its day is over (days from
    ``inp.today`` on are not judged); several rides on one day: only the longest carries the
    plan, as in the per-ride report.
    """
    off = set(unavailable_days(inp, start, end))
    out: list[SessionLine] = []
    for day in days(start, end):
        ours, events = day_plans(inp, day)
        matched: set[tuple[str, int]] = set()
        for row in [a for a in inp.ds.on(day) if a.is_ride]:
            line, key = _ride_line(inp.ride(row))
            out.append(line)
            if key is not None:
                matched.add(key)
        if day >= inp.today:
            continue
        note = text("session.skipped_unavailable") if day in off else ""
        for pw in ours:
            if ("pw", pw.id) not in matched:
                out.append(
                    SessionLine(
                        day=day,
                        planned_name=pw.name,
                        planned_tss=pw.target_tss,
                        verdict="skipped",
                        note_zh=note,
                    )
                )
        for ev in events:
            if ("ev", ev.id) not in matched:
                out.append(
                    SessionLine(
                        day=day,
                        planned_name=ev.name or "",
                        planned_tss=ev.icu_training_load,
                        verdict="skipped",
                        note_zh=note,
                    )
                )
    return out


# ------------------------------------------------------------------------------- load


def _ctl(inp: DigestInputs, day: dt.date) -> tuple[float | None, float | None, float | None]:
    f = inp.ds.fitness.get(day)
    if f is None:
        return None, None, None
    return f.ctl, f.atl, f.tsb


def _r1(v: float | None) -> float | None:
    return round(v, 1) if v is not None else None


def plan_path(inp: DigestInputs, start: dt.date, end: dt.date) -> list[tuple[dt.date, float]]:
    """CTL the season plan projects at each week end in (``start`` − 7 d, ``end``].

    The projection starts from the CTL going into the season and assumes every week done as
    planned (:func:`~cyp.planning.season.season_projection`, as the season overview).
    """
    sk, cfg = inp.skeleton, inp.cfg
    if sk is None or cfg is None:
        return []
    ctl0, _ = inp.ds.ctl_atl(sk.start - dt.timedelta(days=1))
    proj = season_projection(sk, ctl0, weekly_max_minutes=cfg.availability.weekly_max_minutes)
    lo = start - dt.timedelta(days=7)
    out = [(sk.start - dt.timedelta(days=1), round(ctl0, 1))]
    out += [(t.week.end, round(t.ctl_end, 1)) for t in proj]
    return [(d, v) for d, v in out if lo < d <= end]


def build_load(
    inp: DigestInputs,
    start: dt.date,
    end: dt.date,
    sessions: Sequence[SessionLine],
    *,
    warnings: Sequence[Mapping[str, Any]] = (),
    with_plan_path: bool = False,
) -> Load:
    """Section 3: planned (the sessions' plans) vs actual load.

    Actual is the daily load of :attr:`Dataset.loads`, the ledger the CTL is built from
    (intervals.icu's own daily fitness load where it has one, ADR-0003), so it can differ from
    the sum of the rides' loads; the explanation names the difference.
    """
    ds = inp.ds
    planned_by_day: dict[dt.date, float] = {}
    for s in sessions:
        if s.planned_tss is not None:
            planned_by_day[s.day] = planned_by_day.get(s.day, 0.0) + s.planned_tss
    daily = [
        (d, round(planned_by_day.get(d, 0.0), 1), round(ds.loads.get(d, 0.0), 1))
        for d in days(start, end)
    ]
    planned = round(sum(p for _, p, _ in daily), 1)
    actual = round(sum(a for _, _, a in daily), 1)
    rides = inp.rides(start, end)
    ride_load = sum(a.load for a in rides)
    hours = round(sum(a.moving_s for a in rides) / 3600, 1)
    ctl_start = _r1(_ctl(inp, start - dt.timedelta(days=1))[0])
    ctl_end, atl_end, tsb_end = (_r1(v) for v in _ctl(inp, end))
    n_days = (end - start).days + 1
    ramp = (
        round((ctl_end - ctl_start) * 7 / n_days, 1)
        if ctl_start is not None and ctl_end is not None
        else None
    )
    phase = inp.phase_of(end)
    cap = (
        float(inp.cfg.planner.ramp_cap.get(phase, DEFAULT_RAMP_CAP))
        if inp.cfg is not None and phase is not None
        else None
    )
    series = [(d, round(c, 1)) for d in days(start, end) if (c := _ctl(inp, d)[0]) is not None]
    ratio = round(actual / planned, 3) if planned else None

    reasons = [
        Reason(
            text_zh=(
                text("load.vs_plan", actual=actual, planned=planned, ratio=ratio)
                if ratio is not None
                else text("load.no_plan", actual=actual)
            ),
            evidence={"actual_tss": actual, "planned_tss": planned, "ratio": ratio},
        ),
        Reason(
            text_zh=text("load.volume", rides=len(rides), hours=hours),
            evidence={"rides": len(rides), "hours": hours},
        ),
    ]
    other = round(actual - ride_load, 1)
    if abs(other) >= 1:
        key = "load.ledger_more" if other > 0 else "load.ledger_less"
        reasons.append(
            Reason(
                text_zh=text(key, ride=ride_load, diff=abs(other)),
                evidence={"ride_tss": round(ride_load, 1), "ledger_minus_rides": other},
            )
        )
    if ctl_start is not None and ctl_end is not None and ramp is not None:
        reasons.append(
            Reason(
                text_zh=text("load.ctl", start=ctl_start, end=ctl_end, ramp=ramp),
                evidence={"ctl_start": ctl_start, "ctl_end": ctl_end, "ramp_per_week": ramp},
            )
        )
        if cap is not None:
            key = "load.ramp_over" if ramp > cap else "load.ramp_ok"
            reasons.append(
                Reason(
                    text_zh=text(key, ramp=ramp, cap=cap, phase=PHASE_ZH.get(phase or "", "")),
                    evidence={"ramp_per_week": ramp, "ramp_cap": cap, "phase": phase},
                )
            )
    if tsb_end is not None:
        reasons.append(
            Reason(
                text_zh=text("load.tsb", atl=atl_end, tsb=tsb_end),
                evidence={"atl_end": atl_end, "tsb_end": tsb_end},
            )
        )
    for w in warnings:
        reasons.append(
            Reason(
                text_zh=str(w["text_zh"]),
                evidence={"claim": w.get("claim"), "weak": w.get("weak")},
            )
        )
    path = plan_path(inp, start, end) if with_plan_path else []
    if path and ctl_end is not None:
        plan_end = path[-1][1]
        reasons.append(
            Reason(
                text_zh=text("load.plan_path", day=path[-1][0], plan=plan_end, ctl=ctl_end),
                evidence={"plan_ctl": plan_end, "ctl_end": ctl_end},
            )
        )
    headline = (
        text("load.headline", actual=actual, planned=planned, ratio=ratio)
        if ratio is not None
        else text("load.headline_no_plan", actual=actual)
    )
    if ctl_start is not None and ctl_end is not None:
        headline += text("load.headline_ctl", start=ctl_start, end=ctl_end)
    expl = Explanation(
        key=f"digest.load.{start.isoformat()}",
        headline_zh=headline,
        because=reasons,
        method=MethodRef(
            model_id="banister_pmc",
            version="pmc_v1",
            inputs={"start": start.isoformat(), "end": end.isoformat(), "ramp_cap": cap},
            doc="docs/glossary/banister_pmc.md",
            claim="banister_pmc.load_history",
        ),
        confidence="high" if ctl_end is not None else "low",
        glossary_terms=["banister_pmc", "coggan_np_if_tss", "ramp_rate_acwr_monotony"],
    )
    return Load(
        planned_tss=planned,
        actual_tss=actual,
        hours=hours,
        rides=len(rides),
        ctl_start=ctl_start,
        ctl_end=ctl_end,
        atl_end=atl_end,
        tsb_end=tsb_end,
        ramp_per_week=ramp,
        ramp_cap=cap,
        daily=daily,
        ctl_series=series,
        ctl_plan=path,
        explanation=expl.to_json_dict(),
    )


# ------------------------------------------------------------------------------- TID


def tid_verdict(
    sessions: Mapping[Band, int], target: tuple[float, float, float] | None
) -> tuple[str, float | None]:
    """``under`` / ``on`` / ``over`` the phase's hard-session share, and the expected count."""
    n = sum(sessions.values())
    if target is None or n == 0:
        return "no_target", None
    expected = round((target[1] + target[2]) * n, 1)
    hard = sessions.get("mid", 0) + sessions.get("high", 0)
    if hard > expected + TID_SESSION_SLACK:
        return "over", expected
    if hard < expected - TID_SESSION_SLACK:
        return "under", expected
    return "on", expected


def build_distribution(inp: DigestInputs, ws: dt.date) -> Distribution | None:
    """Sessions per zone of the ISO week starting ``ws`` vs its phase target (``None``: no ride).

    Read from :func:`~cyp.analysis.longitudinal.run.compute_trends` as of the week's Sunday,
    whose TID explanation for that week is the one kept.
    """
    tr = inp.trends_at(ws + dt.timedelta(days=6))
    row = next((w for w in tr.report.tid_weeks if w["week_start"] == ws), None) if tr else None
    if tr is None or row is None:
        return None
    sessions: dict[Band, int] = {b: int(row["sessions"][b]) for b in BANDS}
    phase = inp.phase_of(ws)
    target = tid_mod.PHASE_TARGETS.get(phase or "")
    verdict, expected = tid_verdict(sessions, target)
    expl = dict(tr.report.find(f"tid.{ws.isoformat()}") or {})
    if expl and expected is not None:
        hard = sessions["mid"] + sessions["high"]
        rule = Reason(
            text_zh=text(
                "tid.rule",
                hard=hard,
                n=sum(sessions.values()),
                expected=expected,
                slack=TID_SESSION_SLACK,
                verdict=text(f"tid.{verdict}"),
            ),
            evidence={
                "hard_sessions": hard,
                "expected_hard": expected,
                "slack": TID_SESSION_SLACK,
                "verdict": verdict,
            },
        )
        expl["because"] = [*expl.get("because", []), rule.model_dump(mode="json")]
    return Distribution(
        sessions=sessions,
        session_frac={b: float(row[f"session_{b}"]) for b in BANDS},
        target_frac=dict(zip(BANDS, target, strict=True)) if target else None,
        time_frac={b: float(row[b]) for b in BANDS},
        verdict=verdict,  # type: ignore[arg-type]
        explanation=expl or None,
    )


def iso_weeks(start: dt.date, end: dt.date) -> list[dt.date]:
    """Mondays of the ISO weeks overlapping ``[start, end]``."""
    out = []
    ws = week_start(start)
    while ws <= end:
        out.append(ws)
        ws += dt.timedelta(days=7)
    return out
