"""Shared planner fixtures: the real athlete config and template library."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyp.planning.planner import PlanContext
from cyp.planning.season import SeasonSkeleton, build_skeleton
from cyp.planning.templates import Template, load_library
from cyp.settings import AthleteConfig, load_athlete_config

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def cfg() -> AthleteConfig:
    return load_athlete_config(REPO / "config" / "athlete.yaml")


@pytest.fixture(scope="session")
def library() -> dict[str, Template]:
    return load_library()


@pytest.fixture(scope="session")
def skeleton(cfg: AthleteConfig) -> SeasonSkeleton:
    return build_skeleton(cfg)


@pytest.fixture
def ctx(cfg: AthleteConfig, library: dict[str, Template], skeleton: SeasonSkeleton) -> PlanContext:
    return PlanContext(cfg=cfg, library=library, goal_date=skeleton.goal_date)
