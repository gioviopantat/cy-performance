"""Ride classification from time in zone + IF (docs/04 §2).

Classes: ``recovery | endurance | tempo | sweetspot | threshold | vo2 | race | mixed``.
Rules (fractions are of *moving* time; first match wins):

1. ``race`` when the activity is flagged as a race.
2. ``vo2`` when Z5+ >= 15 % of moving time, or >= 10 % with IF >= 0.85.
3. ``threshold`` when Z4 >= 25 % or IF >= 0.92.
4. ``sweetspot`` when IF in [0.84, 0.92) and Z3+Z4 >= 25 %.
5. ``tempo`` when IF in [0.76, 0.84) or Z3 >= 25 %.
6. ``recovery`` when IF < 0.60 or (IF < 0.65 and moving < 60 min).
7. ``endurance`` when Z1+Z2 >= 70 % and IF < 0.80.
8. ``mixed`` otherwise.

Without time in zone (no power, no estimate) only IF-based rules apply; without IF the
result is ``mixed``.
"""

from __future__ import annotations

from typing import Literal

from cyp.analysis.ride.power import tiz_three_zone

RideClass = Literal[
    "recovery", "endurance", "tempo", "sweetspot", "threshold", "vo2", "race", "mixed"
]

CLASS_ZH: dict[str, str] = {
    "recovery": "恢復騎",
    "endurance": "耐力騎",
    "tempo": "節奏騎（Tempo）",
    "sweetspot": "甜蜜點",
    "threshold": "閾值課",
    "vo2": "最大攝氧間歇",
    "race": "比賽",
    "mixed": "混合強度",
}

VO2_HIGH_FRACTION = 0.15
VO2_HIGH_FRACTION_HARD = 0.10
VO2_MIN_IF_HARD = 0.85
THRESHOLD_Z4_FRACTION = 0.25
THRESHOLD_MIN_IF = 0.92
SWEETSPOT_MIN_IF = 0.84
SWEETSPOT_MID_FRACTION = 0.25
TEMPO_MIN_IF = 0.76
TEMPO_Z3_FRACTION = 0.25
RECOVERY_MAX_IF = 0.60
RECOVERY_SHORT_MAX_IF = 0.65
RECOVERY_SHORT_S = 3600
ENDURANCE_LOW_FRACTION = 0.70
ENDURANCE_MAX_IF = 0.80


def classify_ride(
    tiz: dict[str, float] | None,
    if_: float | None,
    *,
    moving_s: int,
    race: bool = False,
) -> RideClass:
    """Classify a ride; see module docstring for the rules."""
    if race:
        return "race"
    total = sum(tiz.values()) if tiz else 0.0
    frac = {k: v / total for k, v in tiz.items()} if tiz and total > 0 else {}
    three = tiz_three_zone(frac) if frac else {"low": 0.0, "mid": 0.0, "high": 0.0}
    z3 = frac.get("Z3", 0.0)
    z4 = frac.get("Z4", 0.0)
    has_tiz = bool(frac)

    if has_tiz and (
        three["high"] >= VO2_HIGH_FRACTION
        or (three["high"] >= VO2_HIGH_FRACTION_HARD and if_ is not None and if_ >= VO2_MIN_IF_HARD)
    ):
        return "vo2"
    if (has_tiz and z4 >= THRESHOLD_Z4_FRACTION) or (if_ is not None and if_ >= THRESHOLD_MIN_IF):
        return "threshold"
    if (
        if_ is not None
        and SWEETSPOT_MIN_IF <= if_ < THRESHOLD_MIN_IF
        and (not has_tiz or three["mid"] >= SWEETSPOT_MID_FRACTION)
    ):
        return "sweetspot"
    if (if_ is not None and TEMPO_MIN_IF <= if_ < SWEETSPOT_MIN_IF) or (
        has_tiz and z3 >= TEMPO_Z3_FRACTION
    ):
        return "tempo"
    if if_ is not None and (
        if_ < RECOVERY_MAX_IF or (if_ < RECOVERY_SHORT_MAX_IF and moving_s < RECOVERY_SHORT_S)
    ):
        return "recovery"
    endurance_if = if_ is None or if_ < ENDURANCE_MAX_IF
    if has_tiz and three["low"] >= ENDURANCE_LOW_FRACTION and endurance_if:
        return "endurance"
    return "mixed"
