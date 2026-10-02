"""CP/W′ fits on synthetic power-duration data, icu comparison and FTP proposals."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from cyp.analysis.longitudinal import pdc

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "intervals"
CP, WP, PMAX = 270.0, 20000.0, 1000.0
DURS = (5, 15, 30, 60, 120, 180, 300, 600, 1200, 1800, 3600)


def _morton(t: float) -> float:
    return CP + WP / (t + WP / (PMAX - CP))


def _two_param(t: float) -> float:
    return CP + WP / t


def test_mmp_from_rides_takes_best_per_duration() -> None:
    mmp = pdc.mmp_from_rides(
        [
            {"60": 400, "300": 300},
            {"watts": {"60": 420, "300": 290, "1200": "bad"}, "w_kg": {"60": 6.5}},
            None,
            {"5": -1},
        ]
    )
    assert mmp == {60: 420.0, 300: 300.0}


def test_fit_2p_recovers_exact_hyperbola() -> None:
    mmp = {t: _two_param(t) for t in DURS}
    fit = pdc.fit_cp_2p(mmp)
    assert fit is not None
    assert fit.cp == pytest.approx(CP, rel=1e-6)
    assert fit.w_prime == pytest.approx(WP, rel=1e-6)
    assert [d for d, _ in fit.points] == [180, 300, 600, 1200]
    assert fit.r2 == pytest.approx(1.0)
    assert fit.eftp == pytest.approx(CP * pdc.CP_TO_FTP)


def test_fit_3p_recovers_morton() -> None:
    mmp = {t: _morton(t) for t in DURS}
    fit = pdc.fit_cp_3p(mmp)
    assert fit is not None
    assert fit.cp == pytest.approx(CP, rel=0.01)
    assert fit.w_prime == pytest.approx(WP, rel=0.03)
    assert fit.p_max == pytest.approx(PMAX, rel=0.03)


def test_fits_need_enough_points() -> None:
    assert pdc.fit_cp_2p({300: 300.0}) is None
    assert pdc.fit_cp_3p({60: 400.0, 300: 300.0, 1200: 270.0}) is None
    # Non-physical (power rising with duration) -> rejected.
    assert pdc.fit_cp_2p({180: 200.0, 1200: 400.0}) is None


def test_compare_with_icu_fixture_model() -> None:
    model = json.loads((FIXTURES / "mmp_model.json").read_text())
    fit = pdc.fit_cp_2p({t: _two_param(t) for t in DURS})
    assert fit is not None
    cmp = pdc.compare_with_icu(fit, model)
    assert cmp.icu_cp == 262 and cmp.icu_eftp == 258
    assert cmp.cp_diff_pct == pytest.approx((CP - 262) / 262 * 100)
    e = pdc.explain_fit(fit, cmp, as_of=dt.date(2026, 10, 2))
    assert e.key == "pdc.cp_2p.2026-10-02"
    assert any("intervals.icu" in r.text_zh for r in e.because)


AS_OF = dt.date(2026, 10, 2)


def _series(values: list[float]) -> dict[dt.date, float]:
    """Values oldest -> newest, ending on AS_OF."""
    n = len(values)
    return {AS_OF - dt.timedelta(days=n - 1 - i): v for i, v in enumerate(values)}


def test_ftp_proposal_fires_after_14_days_up() -> None:
    est = _series([266.0] * 10 + [276.0] * 7 + [278.0] * 8)  # 15 days >= +3 %
    prop = pdc.ftp_proposal(265.0, est, as_of=AS_OF, sources=["icu_eftp"])
    assert prop.direction == "up"
    assert prop.days_sustained == 15
    assert prop.proposed_ftp == 278.0
    assert prop.change_pct == pytest.approx((278 - 265) / 265 * 100)
    assert prop.explanation is not None and "不會自動改" in prop.explanation.headline_zh


def test_ftp_proposal_needs_sustained_same_side() -> None:
    short = pdc.ftp_proposal(265.0, _series([280.0] * 10), as_of=AS_OF)
    assert short.proposed_ftp is None and short.days_sustained == 10
    flip = _series([250.0] * 10 + [280.0] * 10)
    assert pdc.ftp_proposal(265.0, flip, as_of=AS_OF).days_sustained == 10
    within = pdc.ftp_proposal(265.0, _series([270.0] * 30), as_of=AS_OF)
    assert within.proposed_ftp is None and within.days_sustained == 0
    down = pdc.ftp_proposal(265.0, _series([250.0] * 20), as_of=AS_OF)
    assert down.direction == "down" and down.proposed_ftp == 250.0
    with pytest.raises(ValueError):
        pdc.ftp_proposal(0, {}, as_of=AS_OF)


def test_up_proposal_needs_supporting_20min() -> None:
    est = _series([280.0] * 20)
    weak = pdc.ftp_proposal(265.0, est, as_of=AS_OF, best_20min_w=270.0)  # 0.95*270 = 256
    assert weak.proposed_ftp is None and weak.unsupported and weak.direction == "none"
    assert weak.explanation is not None and "撐不起" in weak.explanation.because[-1].text_zh
    ok = pdc.ftp_proposal(265.0, est, as_of=AS_OF, best_20min_w=290.0)  # 275.5 >= 271.6
    assert ok.proposed_ftp == 280.0 and not ok.unsupported
    down = pdc.ftp_proposal(265.0, _series([250.0] * 20), as_of=AS_OF, best_20min_w=200.0)
    assert down.proposed_ftp == 250.0  # downward proposals need no supporting effort


def test_daily_series_forward_fills() -> None:
    pts = {dt.date(2026, 9, 1): 260.0, dt.date(2026, 9, 5): 265.0}
    s = pdc.daily_series(pts, dt.date(2026, 9, 3), dt.date(2026, 9, 6))
    assert s == {
        dt.date(2026, 9, 3): 260.0,
        dt.date(2026, 9, 4): 260.0,
        dt.date(2026, 9, 5): 265.0,
        dt.date(2026, 9, 6): 265.0,
    }
