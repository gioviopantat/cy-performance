"""Personal scales (analysis/personal_scales.py) and how readiness reads them (ADR-0019)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from cyp.analysis import personal_scales as ps
from cyp.analysis.readiness import (
    ALGO_VERSION_FEEL,
    ALGO_VERSION_PERSONAL,
    ReadinessInputs,
    YesterdayRide,
    compute_readiness,
    expected_rpe,
    feel_bad_at,
)
from cyp.dataset import ActivityRow, Dataset

AS_OF = dt.date(2026, 10, 17)


def _ride(i: int, days_ago: int, cls: str, *, rpe: int | None = None, feel: int | None = None,
          minutes: int = 90, load: float = 60.0) -> ActivityRow:  # fmt: skip
    return ActivityRow(
        id=i,
        date=AS_OF - dt.timedelta(days=days_ago),
        sport="Ride",
        name=None,
        is_ride=True,
        moving_s=minutes * 60,
        kj=None,
        has_power=True,
        has_hr=True,
        load=load,
        load_source="icu",
        rpe=rpe,
        feel=feel,
        analysed=True,
        classification=cls,
    )


def _ds(rides: list[ActivityRow]) -> Dataset:
    loads: dict[dt.date, float] = {}
    for r in rides:
        loads[r.date] = loads.get(r.date, 0.0) + r.load
    return Dataset(
        version="t",
        athlete_id=1,
        weight_kg=None,
        settings=(),
        activities=tuple(sorted(rides, key=lambda r: r.date)),
        loads=loads,
        wellness={},
        fitness={},
        readiness={},
        events=(),
        icu_models={},
    )


# ----------------------------------------------------------------------------- RPE offset


def test_rpe_offset_per_class_is_shrunk_and_clipped() -> None:
    # Six Z2 rides rated 5 (table 3): median +2, shrunk 6·2/(6+5) = 1.09.
    rides = [_ride(i, i, "endurance", rpe=5) for i in range(1, 7)]
    got = ps.learn_rpe_offsets(_ds(rides), AS_OF, None)
    assert got["endurance"] == ps.Learned(1.09, 6, "learned", 2.0)
    # Fewer than 5 rides in a class: the all-class offset applies.
    assert got["vo2"] == got[ps.ALL]
    # A huge gap with many rides is clipped to ±2.
    many = [_ride(i, i % 80 + 1, "endurance", rpe=10) for i in range(60)]
    assert ps.learn_rpe_offsets(_ds(many), AS_OF, None)["endurance"].value == ps.RPE_OFFSET_MAX


def test_rpe_offset_needs_data_window_and_yaml_wins() -> None:
    few = [_ride(i, i, "endurance", rpe=5) for i in range(1, 5)]
    assert ps.learn_rpe_offsets(_ds(few), AS_OF, None)["endurance"].source == "default"
    old = [_ride(i, ps.RPE_WINDOW_DAYS + i, "endurance", rpe=9) for i in range(1, 9)]
    assert ps.learn_rpe_offsets(_ds(old), AS_OF, None)["endurance"].value == 0.0
    rides = [_ride(i, i, "endurance", rpe=5) for i in range(1, 9)]
    got = ps.learn_rpe_offsets(_ds(rides), AS_OF, None, {"endurance": -1.0})
    assert got["endurance"] == ps.Learned(-1.0, 0, "yaml")
    # A written all-class offset is also the fallback of the classes without enough rides.
    every = ps.learn_rpe_offsets(_ds(rides), AS_OF, None, {"all": 1.5})
    assert every["vo2"] == every[ps.ALL] == ps.Learned(1.5, 0, "yaml")
    assert every["endurance"].source == "learned"
    # The web answer wins over the icu field.
    web = ps.learn_rpe_offsets(_ds(rides), AS_OF, {r.id: (3, None) for r in rides})
    assert web["endurance"].value == 0.0


def test_negative_offset_for_an_athlete_who_rates_low() -> None:
    rides = [_ride(i, i, "endurance", rpe=1) for i in range(1, 11)]
    got = ps.learn_rpe_offsets(_ds(rides), AS_OF, None)
    assert got["endurance"].value == pytest.approx(-2 * 10 / 15, abs=0.01)


# ------------------------------------------------------------------------- feel baseline


def test_feel_baseline_needs_eight_answers() -> None:
    seven = [_ride(i, i, "endurance", feel=4) for i in range(1, 8)]
    assert ps.learn_feel_baseline(_ds(seven), AS_OF, None).source == "default"
    eight = [*seven, _ride(9, 9, "endurance", feel=4)]
    assert ps.learn_feel_baseline(_ds(eight), AS_OF, None) == ps.Learned(4.0, 8, "learned", 4.0)
    assert ps.learn_feel_baseline(_ds(eight), AS_OF, None, 2.0).source == "yaml"


# --------------------------------------------------------------------------- easy day


def test_easy_day_is_shrunk_bounded_and_skips_hard_days() -> None:
    default = 43.5
    easy = [_ride(i, i, "endurance", load=90.0) for i in range(1, 8)]
    hard = [_ride(20 + i, i, "vo2", load=30.0) for i in (1, 2)]  # same days: not easy days
    got = ps.learn_easy_day_tss(_ds(easy + hard), AS_OF, default)
    assert got.n == 5 and got.source == "default"  # two days turned hard by the VO2 ride
    seven = ps.learn_easy_day_tss(_ds(easy), AS_OF, default)
    assert seven.n == 7 and seven.raw == 90.0
    assert seven.value == pytest.approx((7 * 90 + 5 * default) / 12, abs=0.1)
    huge = [_ride(i, i, "endurance", load=400.0) for i in range(1, 28)]
    assert ps.learn_easy_day_tss(_ds(huge), AS_OF, default).value == pytest.approx(
        ps.EASY_MAX_FACTOR * default, abs=0.1
    )
    tiny = [_ride(i, i, "endurance", load=10.0) for i in range(1, 28)]
    assert ps.learn_easy_day_tss(_ds(tiny), AS_OF, default).value == default
    assert ps.learn_easy_day_tss(_ds(easy), AS_OF, default, 70.0) == ps.Learned(70.0, 0, "yaml")


def test_rows_for_learned_params() -> None:
    rides = [_ride(i, i, "endurance", rpe=5, feel=2) for i in range(1, 10)]
    scales = ps.learn(_ds(rides), AS_OF, None, easy_default_tss=43.5)
    params = {r[0] for r in scales.rows()}
    assert {"rpe_offset.endurance", "rpe_offset.all", "feel_baseline", "easy_day_tss"} <= params
    assert scales.offsets()["endurance"] == scales.offset_for("endurance")
    assert "mixed" not in scales.offsets() and ps.ALL not in scales.offsets()


# ----------------------------------------------------------------------------- readiness


def _ready(y: YesterdayRide, **kw: Any) -> Any:
    return compute_readiness(ReadinessInputs(date=AS_OF, today=None, tsb=0.0, yesterday=y, **kw))


def test_positive_offset_keeps_a_usual_rating_from_capping_today() -> None:
    # Dad-like: every Z2 rated 6. The table reads +3 (cap EASY); his +2 offset reads +1.
    y = YesterdayRide(classification="endurance", rpe=6)
    assert _ready(y).recommendation in ("EASY", "REST")
    personal = _ready(y, rpe_offset={"endurance": 2.0})
    assert personal.recommendation not in ("EASY", "REST")
    assert personal.algo_version == ALGO_VERSION_PERSONAL
    assert any("feel_baseline" in b.evidence for b in personal.explanation.because)


def test_negative_offset_reads_a_usual_three_as_harder() -> None:
    # Roger-like: rates Z2 a 1. A 4 is +1 on the table but +3 for him.
    y = YesterdayRide(classification="endurance", rpe=4)
    assert _ready(y).recommendation not in ("EASY", "REST")
    assert _ready(y, rpe_offset={"endurance": -2.0}).recommendation in ("EASY", "REST")
    # No offset is extrapolated onto a class outside the table.
    mixed = YesterdayRide(classification="mixed", rpe=6)
    assert expected_rpe(mixed, {"endurance": -2.0, "all": -2.0}) == 4.0


def test_feel_baseline_only_relaxes_the_rule() -> None:
    assert feel_bad_at(None) == 5 and feel_bad_at(1.0) == 5 and feel_bad_at(3.5) == 5.5
    weak = YesterdayRide(classification="endurance", feel=5)
    assert _ready(weak).recommendation in ("EASY", "REST")
    assert _ready(weak, feel_baseline=4.0).recommendation not in ("EASY", "REST")
    four = YesterdayRide(classification="endurance", feel=4)
    assert _ready(four, feel_baseline=1.0).recommendation not in ("EASY", "REST")


def test_no_scales_keeps_the_v2_3_label() -> None:
    y = YesterdayRide(classification="endurance", rpe=3)
    assert _ready(y).algo_version == ALGO_VERSION_FEEL
    zero = _ready(y, rpe_offset={"endurance": 0.0, "all": 0.0})
    assert zero.algo_version == ALGO_VERSION_FEEL
    assert not any("feel_baseline" in b.evidence for b in zero.explanation.because)
