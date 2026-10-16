"""Weekly training intensity distribution + polarization index (glossary: time_in_zone_tid).

Two views of the same week (ADR-0014):

- **Sessions** (what the phase targets are compared with, as in Seiler & Kjerland 2006 and the
  session-goal literature): each ride counts once, in the zone of its dominant intensity,
  :func:`session_zone`. A ride the classifier (``analysis/ride/classify.py``) called
  ``recovery`` / ``endurance`` is low; ``tempo`` / ``sweetspot`` / ``threshold`` is mid;
  ``vo2`` / ``race`` is high. A ``mixed`` or unclassified ride falls back on its time in zone:
  high with ≥ :data:`SESSION_HIGH_MIN_S` in Z5+, else mid with ≥ :data:`SESSION_MID_MIN_S` in
  Z3+Z4, else low.
- **Time** (reference only): low = Z1+Z2, mid = Z3+Z4, high = Z5+ seconds. Each ride
  contributes its power TIZ when available, else its HR TIZ (same 3-zone collapse). Time
  fractions are always lower in the high zone than session fractions (a VO2 session is mostly
  warm-up and recoveries), so they are never compared with the targets.

``PI = log10((low / mid) * high * 100)`` with time fractions 0–1 (Treff 2019); ``None`` when
mid or high is 0. PI > 2.0 = polarized. PI and the model label are descriptive only.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cyp.analysis.ride.power import tiz_three_zone
from cyp.core.explain import Explanation, MethodRef, Reason
from cyp.core.timeutil import week_start

Zone3 = Literal["low", "mid", "high"]

TID_VERSION = "tid_v2"
#: Target low/mid/high fractions of *sessions* per phase (docs/05 §2.1, ADR-0014).
PHASE_TARGETS: dict[str, tuple[float, float, float]] = {
    "base": (0.80, 0.15, 0.05),
    "build": (0.75, 0.15, 0.10),
    "threshold": (0.75, 0.05, 0.20),
    "test": (0.80, 0.10, 0.10),
}
#: Ride class -> session zone (classes from ``analysis/ride/classify.py``).
SESSION_ZONE: dict[str, Zone3] = {
    "recovery": "low",
    "endurance": "low",
    "tempo": "mid",
    "sweetspot": "mid",
    "threshold": "mid",
    "vo2": "high",
    "race": "high",
}
#: Fallback for ``mixed`` / unclassified rides: Z5+ seconds that make a high session.
SESSION_HIGH_MIN_S = 600.0
#: Fallback: Z3+Z4 seconds that make a mid session.
SESSION_MID_MIN_S = 1200.0


@dataclass
class RideTiz:
    """One ride's TIZ input."""

    date: dt.date
    tiz: Mapping[str, float]  # {"Z1": s, ...}
    basis: str = "power"
    #: The ride classifier's class; ``None`` when unknown or computed from untrusted power.
    classification: str | None = None


def session_zone(classification: str | None, three: Mapping[str, float]) -> Zone3:
    """The zone a ride counts in as a session (see the module docstring)."""
    zone = SESSION_ZONE.get(classification or "")
    if zone is not None:
        return zone
    if three.get("high", 0.0) >= SESSION_HIGH_MIN_S:
        return "high"
    return "mid" if three.get("mid", 0.0) >= SESSION_MID_MIN_S else "low"


@dataclass
class WeekTid:
    """TID of one ISO week (Monday start)."""

    week_start: dt.date
    low_s: float
    mid_s: float
    high_s: float
    n_rides: int
    low_n: int = 0
    mid_n: int = 0
    high_n: int = 0

    @property
    def total_s(self) -> float:
        """Sum of all three zones."""
        return self.low_s + self.mid_s + self.high_s

    def session_fractions(self) -> tuple[float, float, float]:
        """(low, mid, high) fractions of sessions; zeros when no ride. Compared with targets."""
        n = self.low_n + self.mid_n + self.high_n
        if n <= 0:
            return 0.0, 0.0, 0.0
        return self.low_n / n, self.mid_n / n, self.high_n / n

    def fractions(self) -> tuple[float, float, float]:
        """(low, mid, high) fractions of time; zeros when no time. Reference only."""
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
        zone = session_zone(r.classification, three)
        setattr(w, f"{zone}_n", getattr(w, f"{zone}_n") + 1)
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
    """Explanation of one week's TID: sessions vs the phase target, time as reference."""
    s_low, s_mid, s_high = week.session_fractions()
    low, mid, high = week.fractions()
    because = [
        Reason(
            text_zh=(
                f"以課次計 低／中／高 = {week.low_n} / {week.mid_n} / {week.high_n} 趟"
                f"（{s_low * 100:.0f} / {s_mid * 100:.0f} / {s_high * 100:.0f} %）"
            ),
            evidence={
                "low_n": week.low_n,
                "mid_n": week.mid_n,
                "high_n": week.high_n,
                "low": round(s_low, 3),
                "mid": round(s_mid, 3),
                "high": round(s_high, 3),
            },
        ),
        Reason(
            text_zh=(
                f"以時間計 低／中／高 = {low * 100:.0f} / {mid * 100:.0f} / {high * 100:.0f} %"
                f"（共 {week.total_s / 3600:.1f} 小時；僅供參考，不和目標比）"
            ),
            evidence={
                "time_low": round(low, 3),
                "time_mid": round(mid, 3),
                "time_high": round(high, 3),
                "hours": round(week.total_s / 3600, 2),
            },
        ),
    ]
    pi = week.polarization_index
    if pi is not None:
        because.append(
            Reason(
                text_zh=f"極化指數 PI {pi:.2f}（以時間計；> 2.0 為極化，只是描述）",
                evidence={"pi": round(pi, 3)},
            )
        )
    target = PHASE_TARGETS.get(phase or "")
    if target and week.n_rides:
        mid_delta = (s_mid - target[1]) * 100
        high_delta = (s_high - target[2]) * 100
        because.append(
            Reason(
                text_zh=(
                    f"{phase} 期目標（以課次計）{target[0] * 100:.0f} / {target[1] * 100:.0f} / "
                    f"{target[2] * 100:.0f} %，中強度課差 {mid_delta:+.0f}、"
                    f"高強度課差 {high_delta:+.0f} 個百分點"
                ),
                evidence={
                    "target": list(target),
                    "mid_delta_pp": round(mid_delta, 1),
                    "high_delta_pp": round(high_delta, 1),
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
            inputs={
                "phase": phase,
                "session_high_min_s": SESSION_HIGH_MIN_S,
                "session_mid_min_s": SESSION_MID_MIN_S,
            },
            doc="docs/glossary/time_in_zone_tid.md",
            claim="time_in_zone_tid.phase_targets",
        ),
        confidence="high" if week.n_rides >= 3 else "medium" if week.n_rides else "low",
        glossary_terms=["time_in_zone_tid"],
    )
