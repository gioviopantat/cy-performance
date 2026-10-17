"""Repeat-climb leaderboard keyed by climb fingerprint (docs/04 §3 "Climb repeats").

Input: the ``climbs`` JSON our ride analysis stores per activity (``fingerprint``, ``duration_s``,
``vam_m_h``, ``w_kg``, ``avg_w``, ``avg_hr``…). Output per fingerprint with ≥ ``min_efforts``
efforts: best time, best VAM, latest effort, its rank, and the W/kg trend (least-squares slope
per 30 days) — "am I getting faster on *my* climbs?".
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class ClimbEffort:
    """One ascent of a fingerprinted climb."""

    activity_id: int
    date: dt.date
    duration_s: int
    distance_m: float | None = None
    gain_m: float | None = None
    vam_m_h: float | None = None
    avg_w: float | None = None
    w_kg: float | None = None
    avg_hr: float | None = None


@dataclass
class ClimbBoard:
    """Leaderboard for one fingerprint, efforts sorted fastest first."""

    fingerprint: str
    efforts: list[ClimbEffort] = field(default_factory=list)

    @property
    def best(self) -> ClimbEffort:
        """Fastest effort."""
        return self.efforts[0]

    @property
    def latest(self) -> ClimbEffort:
        """Most recent effort."""
        return max(self.efforts, key=lambda e: (e.date, e.activity_id))

    @property
    def latest_rank(self) -> int:
        """1-based rank of :attr:`latest`."""
        return self.efforts.index(self.latest) + 1

    @property
    def wkg_slope_per_30d(self) -> float | None:
        """Least-squares W/kg change per 30 days over efforts with W/kg."""
        pts = [(e.date.toordinal(), e.w_kg) for e in self.efforts if e.w_kg is not None]
        if len(pts) < 3 or len({p[0] for p in pts}) < 2:
            return None
        x = np.array([p[0] for p in pts], dtype=float)
        y = np.array([p[1] for p in pts], dtype=float)
        slope = float(np.polyfit(x, y, 1)[0])
        return slope * 30.0

    @property
    def distance_m(self) -> float | None:
        """Median distance of the efforts."""
        d = [e.distance_m for e in self.efforts if e.distance_m]
        return float(np.median(d)) if d else None

    @property
    def gain_m(self) -> float | None:
        """Median gain of the efforts."""
        g = [e.gain_m for e in self.efforts if e.gain_m]
        return float(np.median(g)) if g else None


POWER_KEYS = ("avg_w", "w_kg", "np_w", "max_w")


def strip_power(climbs: Any) -> list[dict[str, Any]]:
    """Climbs without their power fields (ride flagged ``power_unreliable``).

    Time, VAM and heart rate stay: they do not depend on the power meter.
    """
    if not isinstance(climbs, list | tuple):
        return []
    return [
        {k: v for k, v in c.items() if k not in POWER_KEYS} for c in climbs if isinstance(c, dict)
    ]


def leaderboard(
    rides: Iterable[tuple[int, dt.date, Any]], *, min_efforts: int = 2
) -> list[ClimbBoard]:
    """Build leaderboards from ``(activity_id, date, climbs_json)`` triples.

    Sorted by number of efforts (most-ridden climb first), then fingerprint.
    """
    boards: dict[str, ClimbBoard] = {}
    for activity_id, date, climbs in rides:
        if not isinstance(climbs, list):
            continue
        for c in climbs:
            if not isinstance(c, dict):
                continue
            fp = c.get("fingerprint")
            dur = c.get("duration_s")
            if not fp or not isinstance(dur, int | float) or dur <= 0:
                continue
            boards.setdefault(fp, ClimbBoard(fp)).efforts.append(
                ClimbEffort(
                    activity_id=activity_id,
                    date=date,
                    duration_s=int(dur),
                    distance_m=c.get("distance_m"),
                    gain_m=c.get("gain_m"),
                    vam_m_h=c.get("vam_m_h"),
                    avg_w=c.get("avg_w"),
                    w_kg=c.get("w_kg"),
                    avg_hr=c.get("avg_hr"),
                )
            )
    out = []
    for b in boards.values():
        if len(b.efforts) < min_efforts:
            continue
        b.efforts.sort(key=lambda e: (e.duration_s, e.date))
        out.append(b)
    out.sort(key=lambda b: (-len(b.efforts), b.fingerprint))
    return out
