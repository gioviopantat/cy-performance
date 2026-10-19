"""Resting-HR source changes are detected; ordinary drift is not."""

from __future__ import annotations

import datetime as dt

from cyp.analysis.wellness_quality import step_change

D0 = dt.date(2026, 9, 1)


def _series(values: list[float]) -> list[tuple[dt.date, float]]:
    return [(D0 + dt.timedelta(days=i), v) for i, v in enumerate(values)]


def test_overnight_jump_is_found() -> None:
    s = step_change(_series([84, 80, 82, 78, 84, 80, 54, 47, 50, 47, 48, 54]))
    assert s is not None and s.day == dt.date(2026, 9, 7)
    assert s.before > 75 and s.after < 55 and s.jump < -20


def test_normal_variation_and_short_series_are_ignored() -> None:
    assert step_change(_series([52, 55, 50, 54, 53, 56, 51, 55, 52, 54])) is None
    assert step_change(_series([80, 50])) is None


def test_one_odd_night_is_not_a_step() -> None:
    assert step_change(_series([52, 53, 51, 75, 52, 54, 53, 52, 51, 53])) is None
