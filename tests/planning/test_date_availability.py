"""One-off date minutes (``availability.dates``): a swapped weekend moves the long ride."""

from __future__ import annotations

import datetime as dt

from cyp.planning.planner import PlanContext, plan_week
from cyp.planning.season import build_skeleton, week_targets
from cyp.planning.templates import Template
from cyp.settings import AthleteConfig
from tests.planning.test_athlete_rules import _cfg


def _week(cfg: AthleteConfig, library: dict[str, Template]) -> list:  # type: ignore[type-arg]
    sk = build_skeleton(cfg)
    w = next(w for w in sk.weeks if w.phase == "base" and not w.recovery)
    ctx = PlanContext(cfg=cfg, library=library, goal_date=sk.goal_date)
    return plan_week(w, week_targets(w, 55.0, weekly_max_minutes=900), ctx)


def _with_dates(cfg: AthleteConfig, dates: dict[dt.date, int]) -> AthleteConfig:
    avail = cfg.availability.model_copy(update={"dates": dates})
    return cfg.model_copy(update={"availability": avail})


def test_swapped_friday_and_saturday_move_the_long_ride(library: dict[str, Template]) -> None:
    cfg = _cfg()
    base = _week(cfg, library)
    sat = next(d.date for d in base if d.role == "long_ride")
    assert sat.weekday() == 5
    fri = sat - dt.timedelta(days=1)
    swapped = {d.date: d for d in _week(_with_dates(cfg, {fri: 240, sat: 90}), library)}
    assert swapped[fri].role == "long_ride"
    assert swapped[sat].role != "long_ride" and swapped[sat].minutes <= 90


def test_dates_outside_the_week_change_nothing(library: dict[str, Template]) -> None:
    cfg = _cfg()
    far = {dt.date(2030, 1, 4): 300}
    assert [(d.date, d.template_id) for d in _week(cfg, library)] == [
        (d.date, d.template_id) for d in _week(_with_dates(cfg, far), library)
    ]
