"""config/athlete.yaml parsing and validation (good and bad inputs)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from cyp.core.errors import ConfigError
from cyp.settings import load_athlete_config


def _mutate(path: Path, **changes: object) -> Path:
    data = yaml.safe_load(path.read_text())
    for dotted, value in changes.items():
        node = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path.write_text(yaml.safe_dump(data))
    return path


def test_repo_config_is_valid(athlete_yaml: Path) -> None:
    cfg = load_athlete_config(athlete_yaml)
    assert cfg.timezone == "Asia/Taipei"
    assert cfg.athlete.ftp_w == 265
    assert cfg.goals[0].kind == "ftp_target"
    assert cfg.goals[0].target == {"ftp": 300}
    assert cfg.goals[0].checkpoints[1].ftp == 285
    assert cfg.season.start == dt.date(2026, 10, 5)
    assert cfg.season.tests.ramp is not None and cfg.season.tests.ramp.every_weeks == 8
    assert cfg.availability.weekly_max_minutes == 900
    assert cfg.availability.per_day["sat"] == 240
    assert cfg.location.weather_indoor_if.rain_prob_pct == 60
    assert cfg.other_sports.plan is False
    assert cfg.planner.mode == "propose"
    assert cfg.planner.today_cutoff_local == dt.time(10, 0)
    assert cfg.planner.ramp_cap["threshold"] == 4
    assert cfg.planner.long_ride_day == "sat"


def test_missing_file() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_athlete_config(Path("does/not/exist.yaml"))


def test_negative_availability_rejected(athlete_yaml: Path) -> None:
    _mutate(athlete_yaml, **{"availability.tue": -10})
    with pytest.raises(ConfigError, match="availability"):
        load_athlete_config(athlete_yaml)


def test_bad_planner_mode_rejected(athlete_yaml: Path) -> None:
    _mutate(athlete_yaml, **{"planner.mode": "maybe"})
    with pytest.raises(ConfigError, match=r"planner\.mode"):
        load_athlete_config(athlete_yaml)


def test_goal_before_season_rejected(athlete_yaml: Path) -> None:
    _mutate(athlete_yaml, **{"season.start": dt.date(2028, 1, 1)})
    with pytest.raises(ConfigError, match="precedes season start"):
        load_athlete_config(athlete_yaml)


def test_unknown_key_rejected(athlete_yaml: Path) -> None:
    _mutate(athlete_yaml, **{"planner.tsb_flor": {"base": -30}})
    with pytest.raises(ConfigError, match="tsb_flor"):
        load_athlete_config(athlete_yaml)


def test_non_mapping_rejected(tmp_path: Path) -> None:
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_athlete_config(p)
