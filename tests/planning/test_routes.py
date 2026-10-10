"""Climb suggestions for long outdoor work steps (planning/routes.py)."""

from __future__ import annotations

from cyp.planning.routes import Climb, footer_line, longest_hard_step_min, suggest
from cyp.planning.templates import Repeat, ResolvedWorkout, Step

CLIMBS = [
    Climb("爬坡 A", 10, 13, 5.3, ("sweetspot", "threshold", "vo2")),
    Climb("大科路", 15, 20, 4.3, ("tempo", "sweetspot", "threshold")),
    Climb("爬坡 B", 17, 22, 3.4, ("sweetspot", "threshold", "test")),
    Climb("106", 22, 25, 2.5, ("endurance", "tempo", "sweetspot", "test")),
]


def _workout(work_min: int, *, outdoor: bool = True, lo: float = 88) -> ResolvedWorkout:
    items = (
        Step("ramp", 600, 50, 70),
        Repeat(3, "Main Set", (Step("work", work_min * 60, lo, lo + 4), Step("rest", 240, 50, 55))),
    )
    return ResolvedWorkout(
        template_id="ss_3x_n",
        template_version=1,
        params={},
        target="POWER",  # type: ignore[arg-type]
        outdoor=outdoor,
        items=items,
        name="SS",
        name_zh="甜蜜點",
        duration_s=3600,
        tss=60.0,
        if_=0.8,
        note_zh=None,
    )


def test_longest_hard_step() -> None:
    assert longest_hard_step_min(_workout(12)) == 12


def test_shortest_fitting_climb_wins() -> None:
    assert suggest(CLIMBS, 8, "sweetspot") == (CLIMBS[0], True)
    assert suggest(CLIMBS, 12, "sweetspot") == (CLIMBS[1], True)
    assert suggest(CLIMBS, 20, "test") == (CLIMBS[3], True)


def test_too_long_names_longest_with_hint() -> None:
    hit = suggest(CLIMBS, 30, "sweetspot")
    assert hit == (CLIMBS[3], False)
    line = footer_line(CLIMBS, _workout(30), "sweetspot")
    assert line is not None and "106" in line and "坡頂掉頭" in line


def test_no_line_indoor_short_or_unmatched() -> None:
    assert footer_line(CLIMBS, _workout(12, outdoor=False), "sweetspot") is None
    assert footer_line(CLIMBS, _workout(5), "sweetspot") is None
    assert footer_line(CLIMBS, _workout(12), "anaerobic") is None
    assert footer_line([], _workout(12), "sweetspot") is None


def test_line_text() -> None:
    assert (
        footer_line(CLIMBS, _workout(12), "sweetspot") == "建議路段：大科路（約 15–20 分，4.3 %）"
    )
