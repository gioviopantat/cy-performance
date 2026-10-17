"""Banister replay, load-safety metrics, simulation and icu agreement."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from cyp.analysis.longitudinal import pmc

D0 = dt.date(2026, 6, 1)


def _loads(n: int) -> dict[dt.date, float]:
    # Weekly pattern: rest Mon, 4 rides, long Sat — deterministic.
    pattern = [0, 60, 90, 50, 80, 150, 110]
    return {D0 + dt.timedelta(days=i): float(pattern[i % 7]) for i in range(n)}


def _icu_reference(loads: dict[dt.date, float], seed: pmc.PMCState, end: dt.date):
    """Independent re-implementation of icu's exponential update (CTL includes today's load)."""
    out = {seed.date: (seed.ctl, seed.atl)}
    ctl, atl, d = seed.ctl, seed.atl, seed.date
    while d < end:
        d += dt.timedelta(days=1)
        load = loads.get(d, 0.0)
        ctl = ctl * math.exp(-1 / 42) + load * (1 - math.exp(-1 / 42))
        atl = atl * math.exp(-1 / 7) + load * (1 - math.exp(-1 / 7))
        out[d] = (round(ctl, 2), round(atl, 2))  # icu reports 2 decimals
    return out


def test_step_matches_closed_form() -> None:
    s = pmc.PMCState(D0, 40.0, 50.0)
    nxt = pmc.step(s, 100.0)
    k42, k7 = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    assert nxt.date == D0 + dt.timedelta(days=1)
    assert nxt.ctl == pytest.approx(40 + 60 * k42)
    assert nxt.atl == pytest.approx(50 + 50 * k7)
    lin = pmc.step(s, 100.0, pmc.PMCParams(decay="linear"))
    assert lin.ctl == pytest.approx(40 + 60 / 42)


def test_replay_tracks_icu_within_tolerance_and_picks_exp() -> None:
    loads = _loads(120)
    seed = pmc.PMCState(D0, 38.5, 42.0)
    end = D0 + dt.timedelta(days=119)
    icu = _icu_reference(loads, seed, end)
    series, agree = pmc.best_params(seed, loads, end, icu)
    assert agree.params.decay == "exp"
    assert agree.within_tolerance
    assert agree.max_abs_ctl_err < 0.01
    assert agree.n_days == 120
    assert series[0].ctl == 38.5 and series[-1].date == end
    linear = pmc.compare_with_icu(
        pmc.replay(seed, loads, end, pmc.PMCParams(decay="linear")), icu, pmc.PMCParams("linear")
    )
    assert linear.mean_abs_ctl_err > agree.mean_abs_ctl_err


def test_safety_metrics() -> None:
    loads = {D0 + dt.timedelta(days=i): 50.0 for i in range(35)}
    day = D0 + dt.timedelta(days=34)
    assert pmc.acwr(loads, day) == pytest.approx(1.0)
    # Double the last week: acute 100 vs chronic 50.
    for i in range(7):
        loads[day - dt.timedelta(days=i)] = 100.0
    assert pmc.acwr(loads, day) == pytest.approx(2.0)
    mono, strain = pmc.monotony_strain(loads, day)
    assert mono is None and strain is None  # identical days: sd 0 -> undefined
    loads[day] = 0.0
    mono, strain = pmc.monotony_strain(loads, day)
    w = [100.0] * 6 + [0.0]
    mean = sum(w) / 7
    sd = (sum((x - mean) ** 2 for x in w) / 7) ** 0.5
    assert mono == pytest.approx(mean / sd)
    assert strain == pytest.approx(600 * mean / sd)
    assert pmc.acwr({}, day) is None


def test_ramp_rate_annotated() -> None:
    loads = _loads(30)
    series = pmc.replay(pmc.PMCState(D0, 40, 40), loads, D0 + dt.timedelta(days=29))
    assert series[6].ramp_rate is None
    assert series[7].ramp_rate == pytest.approx(series[7].ctl - series[0].ctl)


def test_simulate_and_inverse() -> None:
    seed = pmc.PMCState(D0, 45.0, 50.0)
    load = pmc.load_for_ctl_target(seed, 60.0, 42)
    traj = pmc.simulate(seed, [load] * 42)
    assert len(traj) == 42
    assert traj[-1].ctl == pytest.approx(60.0, abs=1e-6)
    assert traj[-1].date == D0 + dt.timedelta(days=42)
    with pytest.raises(ValueError):
        pmc.load_for_ctl_target(seed, 60.0, 0)


def test_explanation_quotes_numbers() -> None:
    loads = _loads(20)
    seed = pmc.PMCState(D0, 40, 40)
    end = D0 + dt.timedelta(days=19)
    series, agree = pmc.best_params(seed, loads, end, _icu_reference(loads, seed, end))
    e = pmc.explain_pmc(series[-1], agree)
    assert e.key == f"pmc.{end.isoformat()}"
    assert e.confidence == "high"
    assert e.because[0].evidence["ctl"] == round(series[-1].ctl, 2)
    assert e.method is not None and e.method.model_id == "banister_pmc"
