"""Goal-aware workout menus (docs/specs/personalized-planning.md).

Pure data plus two tiny functions. ``planner.plan_week`` asks :func:`hit_menu` /
:func:`long_menu` instead of the per-phase constants when ``planner.menus == "goal"``.

- **Emphasis** comes from the goals: a ``distance`` goal means ``endurance`` (tempo, climbs,
  steady long rides), otherwise ``ftp`` (sweet spot -> threshold -> VO2).
- Every stage lists ≥ 2 alternatives where physiology allows, so the limiter bias and the
  ``intensity`` preference have something to choose between.
- ``LIGHT_HIT``: the lighter session for a second hard day only 48 h after the first, for
  athletes ≥ 60 (hard-easy-easy rhythm, docs/05 §2.5).
- Long rides (:func:`long_menu`) come as groups tried in order: finish weeks try the finish
  rides first and only then the steady ones, so variety rotates *within* a group.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from cyp.settings import AthleteConfig

Emphasis = Literal["ftp", "endurance"]
Stage = tuple[str, ...]

TEMPO = ("tempo_3x15", "climb_tempo_repeats", "tempo_with_surges", "low_cadence_sfr")
TEMPO_LONG = ("tempo_2x25", "climb_tempo_repeats", "tempo_3x15")
SWEETSPOT = ("ss_3x_n", "ss_pyramid", "climb_ss_repeats")
SWEETSPOT_LONG = ("ss_3x20", "climb_ss_repeats", "ss_pyramid")
THRESHOLD = ("threshold_4x8_seiler", "threshold_2x15", "over_unders_light")
THRESHOLD_LONG = ("threshold_2x20", "threshold_4x8_seiler", "over_unders_3x_n")
VO2 = ("vo2_30_15_ronnestad", "vo2_4x5", "vo2_40_20")
VO2_LIGHT = ("vo2_30_15_ronnestad", "microbursts_15_15")
LIGHT_HIT: Stage = ("tempo_3x15", "climb_tempo_repeats", "tempo_with_surges", "tempo_2x25")

#: Per emphasis and phase: one list per weekly hard slot; each a list of progression stages.
HIT_MENUS: dict[Emphasis, dict[str, list[list[Stage]]]] = {
    "ftp": {
        "base": [[SWEETSPOT]],
        "build": [
            [THRESHOLD, THRESHOLD, THRESHOLD, THRESHOLD_LONG, THRESHOLD_LONG, THRESHOLD_LONG],
            [
                SWEETSPOT_LONG,
                SWEETSPOT_LONG,
                ("over_unders_3x_n", "climb_repeats_goal_power", "over_unders_light"),
                ("over_unders_3x_n", "climb_repeats_goal_power", "vo2_30_15_ronnestad"),
                ("over_unders_3x_n", "climb_repeats_goal_power", "vo2_30_15_ronnestad"),
                ("over_unders_3x_n", "climb_repeats_goal_power", "vo2_30_15_ronnestad"),
            ],
        ],
        "threshold": [
            [
                VO2,
                THRESHOLD_LONG,
                VO2,
                THRESHOLD_LONG,
                ("vo2_5x5", "vo2_30_15_ronnestad"),
                THRESHOLD_LONG,
            ],
            [
                ("climb_repeats_goal_power", "threshold_2x15"),
                (*VO2_LIGHT, "vo2_30_30_2sets"),
                ("climb_repeats_goal_power", "threshold_4x8_seiler"),
                ("vo2_5x3", "vo2_40_20", "microbursts_15_15"),
                ("climb_repeats_goal_power", "over_unders_3x_n"),
                ("vo2_5x3", "vo2_30_30_2sets", "vo2_30_15_ronnestad"),
            ],
        ],
        "test": [[("vo2_5x3", "microbursts_15_15")]],
    },
    "endurance": {
        "base": [
            [TEMPO, TEMPO, SWEETSPOT, SWEETSPOT, SWEETSPOT, SWEETSPOT],
            [TEMPO, TEMPO, TEMPO_LONG, TEMPO_LONG, TEMPO_LONG, TEMPO_LONG],
        ],
        "build": [
            [SWEETSPOT, SWEETSPOT_LONG, SWEETSPOT_LONG, THRESHOLD, THRESHOLD, THRESHOLD],
            [TEMPO_LONG, TEMPO_LONG, TEMPO, TEMPO_LONG, TEMPO_LONG, TEMPO],
        ],
        # At most one VO2 session every other week: alternate threshold and VO2.
        "threshold": [
            [THRESHOLD, VO2_LIGHT, THRESHOLD_LONG, VO2_LIGHT, THRESHOLD_LONG, THRESHOLD],
            [SWEETSPOT_LONG, TEMPO_LONG, SWEETSPOT_LONG, TEMPO_LONG, SWEETSPOT_LONG, TEMPO_LONG],
        ],
        "test": [[("microbursts_15_15", "tempo_3x15")]],
    },
}

#: Long-ride menus for the endurance emphasis: steady weeks and finish weeks alternate.
LONG_STEADY = ("z2_hilly_180", "z2_endurance_240", "z2_endurance_180")
LONG_FINISH = ("endurance_fast_finish_150", "long_ride_late_tempo", "long_ride_late_ss")

#: ``intensity`` preference -> multiplier on a template's intent score.
INTENSITY_BIAS: dict[str, dict[str, float]] = {
    "easy": {"tempo": 1.3, "sweetspot": 1.15, "threshold": 0.85, "vo2": 0.7},
    "moderate": {},
    "hard": {"tempo": 0.8, "sweetspot": 0.95, "threshold": 1.15, "vo2": 1.25},
}


def emphasis(cfg: AthleteConfig, on: dt.date | None = None) -> Emphasis:
    """``endurance`` with a distance goal not yet past on ``on``, else ``ftp``."""
    return (
        "endurance"
        if any(g.kind == "distance" and (on is None or g.date >= on) for g in cfg.goals)
        else "ftp"
    )


def hit_menu(cfg: AthleteConfig, phase: str, on: dt.date | None = None) -> list[list[Stage]]:
    """Hard-session slots for ``phase`` under this athlete's emphasis."""
    return HIT_MENUS[emphasis(cfg, on)].get(phase, [])


def long_menu(
    cfg: AthleteConfig, phase: str, week_index: int, fallback: Stage, on: dt.date | None = None
) -> list[Stage]:
    """Long-ride groups, tried in order.

    Endurance emphasis alternates steady weeks and finish weeks (finish rides first, steady
    ones only when none fits).
    """
    if emphasis(cfg, on) != "endurance" or phase == "test":
        return [fallback]
    return [LONG_STEADY] if week_index % 2 else [LONG_FINISH, LONG_STEADY]


def intensity_bias(cfg: AthleteConfig) -> dict[str, float]:
    """Intent multipliers for the athlete's ``intensity`` preference."""
    return INTENSITY_BIAS[cfg.planner.intensity]
