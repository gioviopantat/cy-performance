"""Fitness (PMC) series for charts."""

from __future__ import annotations

import datetime as dt

from cyp.schemas import FitnessPoint, FitnessSeries
from cyp.services.context import AppContext


def series(ctx: AppContext, start: dt.date, end: dt.date) -> FitnessSeries:
    """Daily load + CTL/ATL/TSB (icu ledger preferred, our replay otherwise) in ``[start, end]``."""
    ds = ctx.dataset()
    planned = ds.planned_load
    points: list[FitnessPoint] = []
    day = start
    while day <= end:
        f = ds.fitness.get(day)
        w = ds.wellness.get(day)
        ctl = f.ctl if f else (w.ctl if w else None)
        atl = f.atl if f else (w.atl if w else None)
        source = "none"
        if (f is not None and f.ctl_icu is not None) or (w is not None and w.ctl is not None):
            source = "icu"
        elif f is not None and f.ctl_sim is not None:
            source = "sim"
        points.append(
            FitnessPoint(
                date=day,
                load=round(ds.loads.get(day, 0.0), 1),
                ctl=round(ctl, 2) if ctl is not None else None,
                atl=round(atl, 2) if atl is not None else None,
                tsb=round(ctl - atl, 2) if ctl is not None and atl is not None else None,
                source=source,  # type: ignore[arg-type]
                ctl_sim=f.ctl_sim if f else None,
                acwr=f.acwr if f else None,
                monotony=f.monotony if f else None,
                planned_load=planned.get(day),
            )
        )
        day += dt.timedelta(days=1)
    return FitnessSeries(start=start, end=end, points=points)
