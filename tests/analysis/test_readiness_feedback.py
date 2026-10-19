"""Post-ride RPE / feel in the readiness job: one rated ride, per-field precedence."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from cyp.analysis.readiness_job import _yesterday

DAY = dt.date(2026, 10, 8)


def _ride(id_: int, minutes: int, cls: str, rpe: float | None, feel: int | None) -> Any:
    return SimpleNamespace(
        id=id_,
        is_ride=True,
        analysed=True,
        moving_s=minutes * 60,
        classification=cls,
        status=None,
        rpe=rpe,
        feel=feel,
        decoupling_pct=None,
        decoupling_reliable=False,
        hr_lag_s=None,
    )


class _DS:
    def __init__(self, rides: list[Any]) -> None:
        self.rides, self.planned_load, self.loads = rides, {}, {}

    def on(self, day: dt.date) -> list[Any]:
        return self.rides if day == DAY - dt.timedelta(days=1) else []

    def events_between(self, a: dt.date, b: dt.date) -> list[Any]:
        return []


def test_rpe_feel_and_class_come_from_one_ride() -> None:
    ds = _DS([_ride(1, 40, "vo2", None, None), _ride(2, 240, "endurance", 6, None)])
    y = _yesterday(ds, DAY, {})  # type: ignore[arg-type]
    assert y is not None
    assert (y.rpe, y.feel, y.classification) == (6, None, "vo2")
    assert (y.rated_classification, y.rated_minutes) == ("endurance", 240)


def test_web_answer_wins_per_field_and_flag_off_ignores_all() -> None:
    ds = _DS([_ride(1, 90, "tempo", 5, 2)])
    y = _yesterday(ds, DAY, {1: (8, None)})  # type: ignore[arg-type]
    assert y is not None and (y.rpe, y.feel) == (8, 2)
    off = _yesterday(ds, DAY, None)  # type: ignore[arg-type]
    assert off is not None and (off.rpe, off.feel) == (None, None)
