"""Downward FTP proposals: maximal-effort evidence and the 20-min floor."""

from __future__ import annotations

import datetime as dt

from cyp.analysis.longitudinal import limiters as lim
from cyp.analysis.longitudinal.pdc import ftp_proposal

AS_OF = dt.date(2026, 10, 6)


def _est(value: float, days: int = 30) -> dict[dt.date, float]:
    return {AS_OF - dt.timedelta(days=i): value for i in range(days)}


def test_sweet_spot_is_not_maximal_evidence() -> None:
    assert lim.SUSTAINED_FTP_FRAC >= 1.0 and lim.SUSTAINED_LTHR_FRAC >= 0.98


def test_down_withheld_without_maximal_effort() -> None:
    p = ftp_proposal(265, _est(200), as_of=AS_OF, recent_max_effort=False)
    assert p.proposed_ftp is None and p.insufficient_evidence and p.direction == "none"


def test_down_floored_by_recent_20min() -> None:
    p = ftp_proposal(265, _est(200), as_of=AS_OF, recent_max_effort=True, best_20min_w=260)
    assert p.proposed_ftp == 247.0 and p.direction == "down"
    assert any("20 分鐘最佳 260 W" in n for n in p.notes_zh)


def test_down_dropped_when_floor_near_current() -> None:
    p = ftp_proposal(265, _est(200), as_of=AS_OF, recent_max_effort=True, best_20min_w=280)
    assert p.proposed_ftp is None and p.direction == "none"
