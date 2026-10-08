"""Athlete rules: onboarding answers -> planner defaults (docs/specs/age-health-and-distance-goals).

Pure: ``AthleteConfig`` in, effective ``AthleteConfig`` + zh-TW notes out. Applied once, in
``AppContext.athlete_config()``, behind the flags ``plan.athlete_rules`` and
``plan.long_ride_progression`` (ADR-0008). A config without ``birth_year`` / ``health_flags``
/ a distance goal comes back unchanged.

- Age ≥ 60: ``load_pattern 2:1``, ramp cap ≤ 4 CTL/week, ≤ 2 hard sessions/week. These are
  defaults: values written explicitly in ``athlete.yaml`` win (tests are only ever scheduled
  when written there, so a senior gets no ramp test unless asked for).
- Any health flag: at most 1 hard session/week and no maximal tests (ramp, 20-min, final
  test). These are safety caps and apply even over explicit values.
- ``plan.athlete_rules`` off: ``birth_year`` and ``health_flags`` are dropped from the
  effective config, so nothing downstream (the planner's masters rule) sees them.
- ``plan.long_ride_progression`` off: the distance progression is dropped
  (``long_ride_max_minutes`` cleared), so the long ride keeps its configured length.
"""

from __future__ import annotations

import datetime as dt

from cyp.settings import AthleteConfig, PlannerConfig, SeasonConfig

SENIOR_AGE = 60
SENIOR_RAMP_CAP = {"base": 4.0, "build": 4.0, "threshold": 3.0}
SENIOR_MAX_HIT = 2
HEALTH_MAX_HIT = 1
PHASES = ("base", "build", "threshold", "test")


def age_on(cfg: AthleteConfig, day: dt.date) -> int | None:
    """Age in whole years during ``day``'s year (``None`` without a birth year)."""
    year = cfg.athlete.birth_year
    return None if year is None else day.year - year


def is_senior(cfg: AthleteConfig, today: dt.date) -> bool:
    """Age rules apply (≥ :data:`SENIOR_AGE` on ``today``; the planner uses the same date)."""
    age = age_on(cfg, today)
    return age is not None and age >= SENIOR_AGE


def effective(
    cfg: AthleteConfig,
    today: dt.date,
    *,
    athlete_rules: bool = True,
    long_ride_progression: bool = True,
    goal_menus: bool = False,
    variety: bool = False,
) -> tuple[AthleteConfig, list[str]]:
    """The config the planner should use, and one zh-TW note per rule that changed it."""
    notes: list[str] = []
    if not athlete_rules and (cfg.athlete.birth_year is not None or cfg.athlete.health_flags):
        basics = cfg.athlete.model_copy(update={"birth_year": None, "health_flags": None})
        cfg = cfg.model_copy(update={"athlete": basics})
    season = cfg.season
    planner = cfg.planner
    availability = cfg.availability
    if athlete_rules:
        age = age_on(cfg, today)
        if age is not None and age >= SENIOR_AGE:
            season, planner, changed = _senior(cfg)
            if changed:
                notes.append(
                    f"{age} 歲：預設每 3 週 1 週恢復週、每週 CTL 增加不超過 4、"
                    f"每週最多 {SENIOR_MAX_HIT} 堂強度課（athlete.yaml 寫明的值優先）"
                )
        if cfg.athlete.health_flags:
            hit = {
                p: min(planner.hit_per_week.get(p, HEALTH_MAX_HIT), HEALTH_MAX_HIT) for p in PHASES
            }
            planner = planner.model_copy(update={"hit_per_week": hit})
            season = season.model_copy(
                update={
                    "tests": season.tests.model_copy(update={"ramp": None, "twenty_min": None}),
                    "final_test": False,
                }
            )
            notes.append(
                "健康問卷有勾選項目：每週最多 1 堂強度課、不排極限測驗；開始高強度前請先讓醫生確認"
            )
    explicit = planner.model_fields_set
    switches: dict[str, object] = {}
    if goal_menus and "menus" not in explicit:
        switches["menus"] = "goal"
    if variety and "variety" not in explicit:
        switches["variety"] = True
    if switches:
        planner = planner.model_copy(update=switches)
    if not long_ride_progression and availability.long_ride_max_minutes is not None:
        availability = availability.model_copy(update={"long_ride_max_minutes": None})
    if season is cfg.season and planner is cfg.planner and availability is cfg.availability:
        return cfg, notes
    out = cfg.model_copy(
        update={"season": season, "planner": planner, "availability": availability}
    )
    return out, notes


def _senior(cfg: AthleteConfig) -> tuple[SeasonConfig, PlannerConfig, bool]:
    """Age defaults for fields the athlete did not set explicitly."""
    season, planner = cfg.season, cfg.planner
    changed = False
    if "load_pattern" not in season.model_fields_set and season.load_pattern != "2:1":
        season = season.model_copy(update={"load_pattern": "2:1"})
        changed = True
    ramp = dict(planner.ramp_cap)
    for phase, cap in SENIOR_RAMP_CAP.items():
        if phase not in ramp:
            ramp[phase] = cap
    hit = dict(planner.hit_per_week)
    for phase in PHASES:
        if phase not in hit:
            hit[phase] = SENIOR_MAX_HIT if phase in ("build", "threshold") else 1
    if ramp != planner.ramp_cap or hit != planner.hit_per_week:
        planner = planner.model_copy(update={"ramp_cap": ramp, "hit_per_week": hit})
        changed = True
    return season, planner, changed
