"""Calendar view: planned vs done per day, whole weeks (docs/specs/calendar-view.md).

Read-only. The planned workout of a day is what the intervals.icu calendar showed (ours or the
athlete's own WORKOUT event), else our live proposal, so past days are judged against what the
athlete actually saw (same rule as ``Dataset.planned_load``).
"""

from __future__ import annotations

import datetime as dt

from cyp.analysis.ride.classify import CLASS_ZH
from cyp.core.errors import ConfigError
from cyp.dataset import ActivityRow, Dataset
from cyp.planning.season import PHASE_ZH, build_skeleton, season_projection
from cyp.planning.templates import TemplateError, load_library, resolve
from cyp.reports.render import REC_ZH
from cyp.schemas import (
    CalendarActivity,
    CalendarDay,
    CalendarOut,
    CalendarPlanned,
    CalendarWeek,
    DayStatus,
)
from cyp.services.context import AppContext

MAX_DAYS = 63
OVER = 1.3  # same thresholds as the planner's compliance rules (planning/adapt.py)
UNDER = 0.5
NOTE_CATEGORIES = {
    "HOLIDAY": "休假",
    "SICK": "生病",
    "INJURED": "受傷",
    "RACE_A": "A 級比賽",
    "RACE_B": "B 級比賽",
    "RACE_C": "C 級比賽",
}


def status_of(day: dt.date, today: dt.date, planned_tss: float | None, load: float) -> DayStatus:
    """Compliance of one day: the colour of its cell."""
    rode = load > 0
    if planned_tss is None:
        if rode:
            return "extra"
        return "rest"
    if day > today:
        return "planned"
    if not rode:
        return "pending" if day == today else "skipped"
    if planned_tss <= 0:
        return "done"
    ratio = load / planned_tss
    if ratio > OVER:
        return "over"
    if ratio < UNDER:
        return "pending" if day == today else "under"
    return "done"


def _planned(ds: Dataset, day: dt.date) -> CalendarPlanned | None:
    events = [e for e in ds.events if e.date == day and e.category == "WORKOUT"]
    rows = ds.planned.get(day, ())
    lib = load_library()
    ours = None
    for row in rows:
        t = lib.get(row.template_id or "")
        if t is None:
            continue
        try:
            ours = (row, resolve(t, dict(row.params), outdoor=not row.indoor))
        except TemplateError:
            continue
        break
    if events:
        e = events[0]
        name = e.name or "課表"
        tss = sum(float(x.load) for x in events if x.load is not None) or None
        # Our own event showing our current proposal: we also know role, minutes and venue.
        if ours is not None and ours[1].name_zh.strip() == name.strip():
            row, w = ours
            return CalendarPlanned(
                name_zh=name,
                source="calendar",
                tss=round(tss if tss is not None else w.tss, 1),
                minutes=round(w.duration_s / 60),
                role=row.role,
                outdoor=w.outdoor,
            )
        return CalendarPlanned(
            name_zh=name, source="calendar", tss=round(tss, 1) if tss is not None else None
        )
    if ours is not None:
        row, w = ours
        return CalendarPlanned(
            name_zh=w.name_zh,
            source="proposal",
            tss=round(w.tss, 1),
            minutes=round(w.duration_s / 60),
            role=row.role,
            outdoor=w.outdoor,
        )
    return None


def _activity(a: ActivityRow) -> CalendarActivity:
    return CalendarActivity(
        id=a.id,
        name=a.name,
        sport=a.sport,
        is_ride=a.is_ride,
        minutes=round(a.moving_s / 60),
        load=round(a.load, 1),
        classification_zh=CLASS_ZH.get(a.classification or "") if a.classification else None,
        status=a.status,
    )


def calendar(ctx: AppContext, start: dt.date, end: dt.date) -> CalendarOut:
    """Whole Monday–Sunday weeks covering ``[start, end]`` (at most :data:`MAX_DAYS` days)."""
    first = start - dt.timedelta(days=start.weekday())
    last = end + dt.timedelta(days=6 - end.weekday())
    if (last - first).days + 1 > MAX_DAYS:
        raise ValueError(f"range too long (max {MAX_DAYS} days)")
    ds = ctx.dataset()
    today = ctx.today()

    days: list[CalendarDay] = []
    d = first
    while d <= last:
        acts = ds.on(d)
        load = sum(a.load for a in acts if a.is_ride)
        planned = _planned(ds, d)
        r = ds.readiness.get(d)
        f = ds.fitness.get(d) if d <= today else None
        notes = [
            NOTE_CATEGORIES.get(e.category, e.name or e.category)
            for e in ds.events
            if e.category in NOTE_CATEGORIES
            and e.date is not None
            and e.date <= d <= (e.end_date or e.date)
        ]
        days.append(
            CalendarDay(
                date=d,
                status=status_of(d, today, (planned.tss or 0.0) if planned else None, load),
                planned=planned,
                activities=[_activity(a) for a in acts],
                load=round(load, 1),
                notes=notes,
                readiness_score=round(r.score, 1) if r and r.score is not None else None,
                readiness_zh=REC_ZH.get(r.recommendation, r.recommendation)
                if r and r.recommendation
                else None,
                ctl=round(f.ctl, 1) if f and f.ctl is not None else None,
                tsb=round(f.tsb, 1) if f and f.tsb is not None else None,
            )
        )
        d += dt.timedelta(days=1)

    return CalendarOut(today=today, start=first, end=last, days=days, weeks=_weeks(ctx, ds, days))


def _weeks(ctx: AppContext, ds: Dataset, days: list[CalendarDay]) -> list[CalendarWeek]:
    targets: dict[dt.date, tuple[float, str, bool]] = {}
    try:
        cfg = ctx.athlete_config()
        sk = build_skeleton(cfg)
        ctl, _ = ds.ctl_atl(min(ctx.today(), sk.start) - dt.timedelta(days=1))
        proj = season_projection(sk, ctl, weekly_max_minutes=cfg.availability.weekly_max_minutes)
        for w, t in zip(sk.weeks, proj, strict=True):
            targets[w.start - dt.timedelta(days=w.start.weekday())] = (
                t.target_tss,
                PHASE_ZH[w.phase],
                w.recovery,
            )
    except (ConfigError, ValueError):  # no athlete.yaml / season yet: the calendar still works
        targets = {}
    out: list[CalendarWeek] = []
    for i in range(0, len(days), 7):
        week = days[i : i + 7]
        start = week[0].date
        moving = sum(a.minutes for c in week for a in c.activities if a.is_ride)
        ctl_end = next((c.ctl for c in reversed(week) if c.ctl is not None), None)
        target = targets.get(start)
        out.append(
            CalendarWeek(
                start=start,
                load_done=round(sum(c.load for c in week), 1),
                load_planned=round(sum((c.planned.tss or 0.0) for c in week if c.planned), 1),
                target_tss=round(target[0], 1) if target else None,
                hours_done=round(moving / 60, 1),
                ctl_end=ctl_end,
                phase_zh=target[1] if target else None,
                recovery=target[2] if target else False,
            )
        )
    return out
