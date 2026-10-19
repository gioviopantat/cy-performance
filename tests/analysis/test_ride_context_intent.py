"""Outdoor context (stops, coasting, heat) and the v1 intent check on hand-built 1 Hz rides."""

from __future__ import annotations

import numpy as np
import pytest

from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.ride.frames import RideFrame
from cyp.analysis.ride.intent import IntentTarget, assess_intent, count_reps
from tests.reports.world import Seg, build_stream


def frame(segs: list[Seg], temp_c: float | None = None) -> RideFrame:
    return RideFrame.from_df(build_stream(segs, temp_c=temp_c))


# ------------------------------------------------------------------------------- context


def test_stops_inside_the_ride_only() -> None:
    f = frame(
        [
            Seg(30, 0.0, stop=True),
            Seg(600, 200.0),
            Seg(40, 0.0, stop=True),
            Seg(600, 200.0),
            Seg(10, 0.0, stop=True),
            Seg(300, 200.0),
            Seg(60, 0.0, stop=True),
        ]
    )
    stops = ctx_mod.find_stops(f)
    # Leading / trailing standstill is not a stop; 10 s is under STOP_MIN_S.
    assert [(s.start_s, s.end_s) for s in stops] == [(630, 670)]


def test_coasting_and_temperature() -> None:
    f = frame([Seg(300, 200.0), Seg(100, 0.0, 10.0, -5.0)], temp_c=20.0)
    assert ctx_mod.coasting_pct(f) == pytest.approx(25.0)
    avg, mx = ctx_mod.temperature(f)
    assert mx == 22.0 and 20.0 < (avg or 0) < 22.0


def test_cool_quiet_ride_has_no_caveats() -> None:
    facts = ctx_mod.outdoor_context(frame([Seg(3600, 200.0)], temp_c=18.0))
    assert facts.stops == [] and facts.caveats == []


def test_heat_stops_and_coasting_caveats_quote_numbers() -> None:
    segs: list[Seg] = []
    for _ in range(6):
        segs += [Seg(400, 200.0), Seg(200, 0.0, 9.0, -4.0), Seg(30, 0.0, stop=True)]
    facts = ctx_mod.outdoor_context(frame(segs, temp_c=30.0))
    codes = {c.code: c.evidence for c in facts.caveats}
    assert set(codes) == {"heat", "stops", "coasting"}
    assert codes["heat"]["limit_c"] == ctx_mod.HEAT_AVG_C
    assert codes["stops"]["stops_n"] == 5  # the last stop ends the ride
    assert codes["coasting"]["coasting_pct"] >= ctx_mod.HIGH_COAST_PCT


def test_no_power_no_coasting() -> None:
    f = RideFrame.from_df(build_stream([Seg(600, 200.0)]).drop("watts"))
    assert ctx_mod.coasting_pct(f) is None


# ------------------------------------------------------------------------------- intent

SS = IntentTarget("work", 212.5, 237.5, 1800, rep_s=600, reps=3)
ONE_REP = IntentTarget("work", 212.5, 237.5, 600, rep_s=600, reps=1)


def run(
    segs: list[Seg], target: IntentTarget = SS, np_w: float | None = None
) -> intent_mod.IntentFacts:
    f = frame(segs)
    assert f.watts is not None
    return assess_intent(target, f.watts, f.moving, grade=f.grade, np_w=np_w)


def test_three_clean_reps_achieved() -> None:
    segs = [Seg(600, 150.0)]
    for _ in range(3):
        segs += [Seg(600, 225.0), Seg(240, 120.0)]
    out = run(segs)
    assert out.reps_counted == 3 and out.verdict == "achieved"
    assert out.achieved_s >= 0.95 * 1800
    assert out.excused == []


def test_red_light_inside_a_rep_is_excused_not_a_failure() -> None:
    segs = [
        Seg(600, 150.0),
        Seg(300, 225.0),
        Seg(60, 0.0, stop=True),
        Seg(300, 225.0),
        Seg(240, 120.0),
        Seg(600, 225.0),
        Seg(240, 120.0),
        Seg(600, 225.0),
    ]
    out = run(segs)
    assert out.reps_counted == 3 and out.verdict == "achieved"
    assert len(out.excused) == 1 and out.excused[0].reason == "stop"


def test_descent_inside_a_rep_is_excused_as_coasting() -> None:
    segs = [Seg(600, 150.0), Seg(300, 225.0), Seg(90, 0.0, 12.0, -6.0), Seg(300, 225.0)]
    out = run(segs, ONE_REP)
    assert out.reps_counted == 1
    assert [e.reason for e in out.excused] == ["coast"]


def test_long_stop_splits_the_rep_but_minutes_still_count() -> None:
    segs = [Seg(600, 150.0), Seg(300, 225.0), Seg(300, 0.0, stop=True), Seg(300, 225.0)]
    out = run(segs, ONE_REP)
    assert out.reps_counted == 0  # two 5-min halves are not one 10-min rep
    assert out.verdict == "achieved"  # but the 10 planned minutes in the band were ridden


def test_scattered_minutes_without_reps_are_partial() -> None:
    segs = [Seg(600, 150.0)] + [Seg(180, 225.0), Seg(200, 120.0)] * 8
    out = run(segs)
    assert out.reps_counted == 0 and out.verdict == "partial"


def test_dips_on_a_climb_stay_in_the_rep() -> None:
    # Outdoor power oscillating around the band floor: 40 s at 200 W every 2 minutes.
    rep = [Seg(80, 235.0), Seg(40, 200.0)] * 5
    out = run([Seg(600, 150.0), *rep, Seg(300, 120.0)], ONE_REP)
    assert out.reps_counted == 1 and out.excused == []


def test_one_ease_off_below_the_old_dip_floor_stays_in_the_rep() -> None:
    """Ride 232 (ADR-0025): 16 min at ~234 W with one 36 s ease-off at 155 W is one rep."""
    target = IntentTarget("work", 225.0, 252.0, 960, rep_s=960, reps=1)
    rep = [Seg(700, 240.0), Seg(36, 155.0), Seg(214, 238.0)]
    out = run([Seg(600, 150.0), *rep, Seg(300, 120.0)], target)
    assert out.reps_counted == 1 and out.verdict == "achieved"


def test_a_set_cut_short_counts_its_minutes() -> None:
    """Ride 232: one full 16-min set and one of 11 min (heat), third skipped: partial."""
    target = IntentTarget("work", 225.0, 252.0, 2880, rep_s=960, reps=3)
    segs = [Seg(600, 150.0), Seg(660, 248.0), Seg(600, 100.0), Seg(960, 236.0), Seg(900, 120.0)]
    out = run(segs, target)
    assert out.reps_counted == 1
    assert out.rep_s_done >= 1500 and out.verdict == "partial"
    # Under half a rep is not a set: a 6-min surge adds nothing.
    short = run([Seg(600, 150.0), Seg(360, 248.0), Seg(600, 100.0)], target)
    assert short.rep_s_done == 0


def test_block_below_the_floor_on_average_does_not_count() -> None:
    rep = [Seg(40, 215.0), Seg(60, 175.0)] * 6
    out = run([Seg(600, 150.0), *rep], ONE_REP)
    assert out.reps_counted == 0


def test_short_reps_keep_their_shape() -> None:
    # 10 x 30/15 at 320 W: smoothing shrinks to a quarter rep, so each 30 s rep is visible.
    segs = [Seg(600, 150.0)] + [Seg(30, 320.0), Seg(15, 100.0)] * 10 + [Seg(300, 120.0)]
    out = run(segs, IntentTarget("work", 300.0, 340.0, 300, rep_s=30, reps=10))
    assert out.reps_counted is not None and out.reps_counted >= 9


def test_coasted_planned_rests_are_not_excused() -> None:
    # 10 x 30/15 coasting through every rest: the rests are planned, not interruptions.
    segs = [Seg(600, 150.0)] + [Seg(30, 320.0), Seg(15, 0.0, 9.0, -3.0)] * 10
    out = run(segs, IntentTarget("work", 300.0, 340.0, 300, rep_s=30, reps=10))
    assert out.excused == []
    assert out.reps_counted is not None and out.reps_counted >= 9


def test_double_length_block_counts_twice() -> None:
    assert count_reps(1200, 600) == 2
    assert count_reps(480, 600) == 1
    assert count_reps(470, 600) == 0


@pytest.mark.parametrize(
    ("over_s", "verdict"),
    [(0, "achieved"), (600, "partial"), (1200, "missed")],
)
def test_ceiling_intent_fails_on_time_above(over_s: int, verdict: str) -> None:
    target = IntentTarget("ceiling", 112.5, 137.5, 2700)
    segs = [Seg(3600 - over_s, 120.0), Seg(over_s, 220.0)] if over_s else [Seg(3600, 120.0)]
    out = run(segs, target)
    assert out.verdict == verdict


def test_short_rises_on_rolling_roads_do_not_fail_an_easy_ride() -> None:
    # Z2 with a 40 s kicker every 5 minutes: the 90 s cap smoothing absorbs them.
    segs = [Seg(260, 160.0), Seg(40, 260.0)] * 12
    out = run(segs, IntentTarget("ceiling", 155.0, 187.5, 3600, floor_w=155.0), np_w=175.0)
    assert out.above_s == 0 and out.verdict == "achieved"


def test_far_too_easy_endurance_is_at_most_partial() -> None:
    target = IntentTarget("ceiling", 155.0, 187.5, 3600, floor_w=155.0)
    out = run([Seg(3600, 110.0)], target, np_w=112.0)
    assert out.too_easy and out.verdict == "partial"


def test_too_short_easy_ride_is_partial() -> None:
    out = run([Seg(1500, 120.0)], IntentTarget("ceiling", 112.5, 137.5, 2700))
    assert out.verdict == "partial"


def test_no_power_is_no_data() -> None:
    out = assess_intent(SS, None, np.ones(10, dtype=bool))
    assert out.verdict == "no_data"


def test_load_verdict() -> None:
    assert intent_mod.load_verdict(None) == "no_data"
    assert intent_mod.load_verdict(1.4) == "achieved"
    assert intent_mod.load_verdict(0.6) == "partial"
    assert intent_mod.load_verdict(0.3) == "missed"
