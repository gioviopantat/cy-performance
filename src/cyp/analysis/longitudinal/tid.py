"""Weekly training intensity distribution + polarization index (glossary: time_in_zone_tid).

Low = Z1+Z2, mid = Z3+Z4, high = Z5+ (power zones; time-based, not session-based). Each ride
contributes its power TIZ when available, else its HR TIZ (same 3-zone collapse).

``PI = log10((low / mid) * high * 100)`` with fractions 0–1 (Treff 2019); ``None`` when mid or
high is 0. PI > 2.0 = polarized.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cyp.analysis.ride.power import tiz_three_zone
from cyp.core.explain import Explanation, MethodRef, Reason

TID_VERSION = "tid_v1"
#: Target low/mid/high fractions per phase (docs/05 §2.1).
PHASE_TARGETS: dict[str, tuple[float, float, float]] = {
    "base": (0.80, 0.15, 0.05),
    "build": (0.75, 0.15, 0.10),
    "threshold": (0.75, 0.05, 0.20),
    "test": (0.80, 0.10, 0.10),
}


@dataclass
class RideTiz:
    """One ride's TIZ input."""

    date: dt.date
    tiz: Mapping[str, float]  # {"Z1": s, ...}
    basis: str = "power"


@dataclass
class WeekTid:
    """TID of one ISO week (Monday start)."""

    week_start: dt.date
    low_s: float
    mid_s: float
    high_s: float
    n_rides: int

    @property
    def total_s(self) -> float:
        """Sum of all three zones."""
        return self.low_s + self.mid_s + self.high_s

    def fractions(self) -> tuple[float, float, float]:
        """(low, mid, high) fractions; zeros when no time."""
        tot = self.total_s
        if tot <= 0:
            return 0.0, 0.0, 0.0
        return self.low_s / tot, self.mid_s / tot, self.high_s / tot

    @property
    def polarization_index(self) -> float | None:
        """Treff 2019 PI."""
        low, mid, high = self.fractions()
        if mid <= 0 or high <= 0 or low <= 0:
            return None
        return math.log10((low / mid) * high * 100.0)

    @property
    def model(self) -> str:
        """``polarized`` / ``pyramidal`` / ``threshold`` / ``unknown`` from the zone ordering."""
        low, mid, high = self.fractions()
        if self.total_s <= 0:
            return "unknown"
        if mid > low:
            return "threshold"
        pi = self.polarization_index
        # Treff 2019: polarized = low > high > mid; PI is undefined when mid is 0.
        if high > mid and (pi is None or pi > 2.0):
            return "polarized"
        return "pyramidal"


def week_start(day: dt.date) -> dt.date:
    """Monday of ``day``'s ISO week."""
    return day - dt.timedelta(days=day.weekday())


def weekly_tid(rides: Iterable[RideTiz]) -> list[WeekTid]:
    """Aggregate rides into ISO weeks, ascending."""
    weeks: dict[dt.date, WeekTid] = {}
    for r in rides:
        three = tiz_three_zone(dict(r.tiz))
        ws = week_start(r.date)
        w = weeks.setdefault(ws, WeekTid(ws, 0.0, 0.0, 0.0, 0))
        w.low_s += three["low"]
        w.mid_s += three["mid"]
        w.high_s += three["high"]
        w.n_rides += 1
    return [weeks[k] for k in sorted(weeks)]


def tiz_from_metrics(
    power_tiz: Any, hr_tiz: Any, icu_zone_times: Any
) -> tuple[dict[str, float], str] | None:
    """Pick a ride's TIZ: our power TIZ -> icu power zone times (list) -> our HR TIZ."""
    if isinstance(power_tiz, dict) and power_tiz:
        return {str(k): float(v) for k, v in power_tiz.items()}, "power"
    if isinstance(icu_zone_times, list) and icu_zone_times:
        return {f"Z{i + 1}": float(v or 0) for i, v in enumerate(icu_zone_times[:7])}, "power_icu"
    if isinstance(hr_tiz, dict) and hr_tiz:
        return {str(k): float(v) for k, v in hr_tiz.items()}, "hr"
    return None


def explain_week(week: WeekTid, phase: str | None = None) -> Explanation:
    """Explanation of one week's TID vs the phase target."""
    low, mid, high = week.fractions()
    because = [
        Reason(
            text_zh=(
                f"低／中／高 = {low * 100:.0f} / {mid * 100:.0f} / {high * 100:.0f} %"
                f"（共 {week.total_s / 3600:.1f} 小時、{week.n_rides} 趟）"
            ),
            evidence={
                "low": round(low, 3),
                "mid": round(mid, 3),
                "high": round(high, 3),
                "hours": round(week.total_s / 3600, 2),
            },
        )
    ]
    pi = week.polarization_index
    if pi is not None:
        because.append(
            Reason(text_zh=f"極化指數 PI {pi:.2f}（> 2.0 為極化）", evidence={"pi": round(pi, 3)})
        )
    target = PHASE_TARGETS.get(phase or "")
    if target:
        because.append(
            Reason(
                text_zh=(
                    f"{phase} 期目標 {target[0] * 100:.0f} / {target[1] * 100:.0f} / "
                    f"{target[2] * 100:.0f} %，中區差 {(mid - target[1]) * 100:+.0f} 個百分點"
                ),
                evidence={
                    "target": list(target),
                    "mid_delta_pp": round((mid - target[1]) * 100, 1),
                },
            )
        )
    model_zh = {
        "polarized": "極化",
        "pyramidal": "金字塔",
        "threshold": "閾值型",
        "unknown": "無資料",
    }[week.model]
    return Explanation(
        key=f"tid.{week.week_start.isoformat()}",
        headline_zh=f"{week.week_start.isoformat()} 這週的強度分布是{model_zh}",
        because=because,
        method=MethodRef(
            model_id="tid_3zone",
            version=TID_VERSION,
            inputs={"phase": phase},
            doc="docs/glossary/time_in_zone_tid.md",
        ),
        confidence="high" if week.n_rides >= 3 else "medium" if week.n_rides else "low",
        glossary_terms=["time_in_zone_tid"],
    )
