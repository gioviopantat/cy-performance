"""Flags off = the pre-flags planner, byte for byte (spec personalized-planning, ADR-0008).

``golden/flags_off_v1.json`` was generated with the planner of commit e1c0ce5 (before goal
menus, variety, preferred hard days, intensity and the age rules) on ``golden/athlete.yaml``.
Regenerate only for a deliberate change to the v1 planner, and say so in the commit.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from cyp.analysis.longitudinal.pmc import PMCState, simulate
from cyp.planning.athlete_rules import effective
from cyp.planning.planner import PlanContext, plan_week
from cyp.planning.season import build_skeleton, week_targets
from cyp.planning.templates import Template
from cyp.settings import AthleteConfig, load_athlete_config

GOLDEN = Path(__file__).parent / "golden"
TODAY = dt.date(2026, 10, 7)


def _rows(cfg: AthleteConfig, library: dict[str, Template]) -> list[list[Any]]:
    sk = build_skeleton(cfg)
    ctx = PlanContext(
        cfg=cfg, library=library, goal_date=sk.goal_date, bias={"vo2": 1.2, "threshold": 0.9}
    )
    ctl = atl = 55.0
    prev = None
    out: list[list[Any]] = []
    for w in sk.weeks:
        t = week_targets(
            w,
            ctl,
            weekly_max_minutes=cfg.availability.weekly_max_minutes,
            prev_loading_tss=prev,
            atl_start=atl,
        )
        wk = plan_week(w, t, ctx)
        out += [[d.date.isoformat(), d.role, d.template_id, d.params, round(d.tss, 1)] for d in wk]
        end = simulate(PMCState(w.start - dt.timedelta(days=1), ctl, atl), [d.tss for d in wk])
        ctl, atl = end[-1].ctl, end[-1].atl
        if not w.recovery and w.phase != "test":
            prev = t.target_tss
    return out


@pytest.mark.parametrize(
    "planner",
    [
        {},
        # Preferences and age answers are ignored while their flags are off.
        {"hit_days": ["thu", "fri"], "intensity": "hard"},
    ],
)
def test_flags_off_reproduce_v1(library: dict[str, Template], planner: dict[str, Any]) -> None:
    raw = load_athlete_config(GOLDEN / "athlete.yaml")
    raw = raw.model_copy(
        update={
            "planner": raw.planner.model_copy(update=planner),
            "athlete": raw.athlete.model_copy(update={"birth_year": 1950}),
        }
    )
    cfg, _ = effective(
        raw, TODAY, athlete_rules=False, long_ride_progression=False, goal_menus=False
    )
    golden = json.loads((GOLDEN / "flags_off_v1.json").read_text("utf-8"))
    assert _rows(cfg, library) == golden
