"""A new profile 60+ gets the age defaults (2:1, no ramp test) because the template leaves them
unset (spec age-health-and-distance-goals)."""

from __future__ import annotations

import datetime as dt

import yaml

from cyp.planning.athlete_rules import effective
from cyp.services.profiles import render_athlete_yaml
from cyp.settings import AthleteConfig

START = dt.date(2026, 10, 12)


def _render(birth_year: int | None) -> AthleteConfig:
    text = render_athlete_yaml(
        weight_kg=72.0,
        ftp_w=210,
        goal_ftp=230,
        season_start=START,
        goal_date=START + dt.timedelta(weeks=26),
        availability={"mon": 0, "tue": 90, "wed": 60, "thu": 90, "fri": 0, "sat": 210, "sun": 90},
        timezone="Asia/Taipei",
        birth_year=birth_year,
        health_flags=[],
        distance_km=160,
    )
    return AthleteConfig.model_validate(yaml.safe_load(text))


def test_senior_template_leaves_age_defaults_open() -> None:
    cfg, _ = effective(_render(1950), START)
    assert cfg.season.load_pattern == "2:1"
    assert cfg.season.tests.ramp is None and cfg.season.tests.twenty_min is not None


def test_younger_athlete_keeps_the_written_defaults() -> None:
    cfg, _ = effective(_render(1990), START)
    assert cfg.season.load_pattern == "3:1" and cfg.season.tests.ramp is not None
