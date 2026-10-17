"""Hard-effort detection: sprints, sustained supra-threshold efforts, interval structure.

Sprints port strava-analyis ``detect_efforts``.

- **Sprint**: contiguous raw-power run >= ``SPRINT_MIN_DURATION_S`` (8 s) above
  ``max(1.2 * FTP (or 250 W), 1.5 * pedalling-only mean power)``.
- **Effort**: contiguous run of 30 s-smoothed power >= ``EFFORT_FTP_FRACTION * FTP`` (1.05)
  lasting >= ``EFFORT_MIN_DURATION_S`` (30 s), i.e. time in the threshold-and-above bands.
  Labelled by the power zone of its average power (``threshold`` / ``vo2`` / ``anaerobic``).
- **Structure**: count, total work time, mean/CV of effort durations and of the recoveries
  between them — a regular CV (< 0.25) means the ride looks like a prescribed interval set.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from cyp.analysis.ride.frames import FloatArray, RideFrame, rolling_mean
from cyp.core.athlete import ZoneModel

SPRINT_MIN_DURATION_S = 8
SPRINT_POWER_MULTIPLIER = 1.5
SPRINT_FTP_MULTIPLIER = 1.2
SPRINT_MIN_ABS_WATTS = 250.0
EFFORT_MIN_DURATION_S = 30
EFFORT_FTP_FRACTION = 1.05
STRUCTURE_REGULAR_CV = 0.25

_ZONE_LABELS = {4: "threshold", 5: "vo2", 6: "anaerobic", 7: "neuromuscular"}


def _runs(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Inclusive ``(start, end)`` index pairs of True runs at least ``min_len`` long."""
    if mask.size == 0:
        return []
    padded = np.concatenate(([False], mask, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    starts, ends = edges[0::2], edges[1::2] - 1
    return [(int(s), int(e)) for s, e in zip(starts, ends, strict=True) if e - s + 1 >= min_len]


def _segment(
    frame: RideFrame,
    power: FloatArray,
    start: int,
    end: int,
    kind: str,
    *,
    ftp: float | None,
    weight_kg: float | None,
    zones: ZoneModel | None,
) -> dict[str, Any]:
    seg = power[start : end + 1]
    avg_w = float(seg.mean())
    max_w = float(seg.max())
    hr: float | None = None
    hr_max: float | None = None
    if frame.hr is not None:
        h = frame.hr[start : end + 1]
        h = h[np.isfinite(h)]
        if h.size:
            hr, hr_max = float(h.mean()), float(h.max())
    zone = zones.zone_for(avg_w) if zones is not None else None
    label = kind
    if kind == "effort" and zone is not None:
        label = _ZONE_LABELS.get(zone.idx, "tempo")
    return {
        "kind": kind,
        "label": label,
        "start_idx": int(start),
        "end_idx": int(end),
        "start_s": int(frame.t_s[start]),
        "duration_s": int(end - start + 1),
        "avg_w": round(avg_w, 1),
        "max_w": round(max_w, 1),
        "pct_ftp": round(avg_w / ftp * 100.0, 1) if ftp else None,
        "w_kg": round(avg_w / weight_kg, 2) if weight_kg else None,
        "zone": zone.idx if zone is not None else None,
        "avg_hr": round(hr, 1) if hr is not None else None,
        "max_hr": hr_max,
    }


def detect_sprints(
    frame: RideFrame,
    *,
    ftp: float | None,
    weight_kg: float | None = None,
    zones: ZoneModel | None = None,
    watts: FloatArray | None = None,
) -> list[dict[str, Any]]:
    """Sprints: raw power above the sprint threshold for >= 8 s."""
    source = frame.watts if watts is None else watts
    if source is None:
        return []
    power = np.nan_to_num(source, nan=0.0)
    pedalling = power[power > 0]
    if pedalling.size == 0:
        return []
    floor = SPRINT_FTP_MULTIPLIER * ftp if ftp else SPRINT_MIN_ABS_WATTS
    threshold = max(floor, SPRINT_POWER_MULTIPLIER * float(pedalling.mean()))
    return [
        _segment(frame, power, s, e, "sprint", ftp=ftp, weight_kg=weight_kg, zones=zones)
        for s, e in _runs(power >= threshold, SPRINT_MIN_DURATION_S)
    ]


def detect_efforts(
    frame: RideFrame,
    *,
    ftp: float | None,
    weight_kg: float | None = None,
    zones: ZoneModel | None = None,
    watts: FloatArray | None = None,
) -> list[dict[str, Any]]:
    """Sustained efforts: 30 s-smoothed power >= 105 % FTP for >= 30 s. Empty without FTP."""
    source = frame.watts if watts is None else watts
    if source is None or not ftp:
        return []
    power = np.nan_to_num(source, nan=0.0)
    p30 = rolling_mean(power, 30)
    p30 = np.nan_to_num(p30, nan=0.0)
    return [
        _segment(frame, power, s, e, "effort", ftp=ftp, weight_kg=weight_kg, zones=zones)
        for s, e in _runs(p30 >= EFFORT_FTP_FRACTION * ftp, EFFORT_MIN_DURATION_S)
    ]


def effort_structure(efforts: list[dict[str, Any]]) -> dict[str, Any]:
    """Interval-structure summary of ``efforts`` (sustained efforts only)."""
    work = [e for e in efforts if e.get("kind") == "effort"]
    if not work:
        return {"count": 0, "work_s": 0, "regular": False}
    durations = np.array([e["duration_s"] for e in work], dtype=np.float64)
    starts = np.array([e["start_s"] for e in work], dtype=np.float64)
    ends = starts + durations
    recoveries = starts[1:] - ends[:-1]

    def _cv(x: np.ndarray) -> float | None:
        if x.size < 2 or x.mean() <= 0:
            return None
        return float(x.std() / x.mean())

    dur_cv = _cv(durations)
    rec_cv = _cv(recoveries)
    regular = (
        len(work) >= 3
        and dur_cv is not None
        and dur_cv < STRUCTURE_REGULAR_CV
        and (rec_cv is None or rec_cv < STRUCTURE_REGULAR_CV)
    )
    return {
        "count": len(work),
        "work_s": int(durations.sum()),
        "mean_duration_s": round(float(durations.mean()), 1),
        "duration_cv": round(dur_cv, 3) if dur_cv is not None else None,
        "mean_recovery_s": round(float(recoveries.mean()), 1) if recoveries.size else None,
        "recovery_cv": round(rec_cv, 3) if rec_cv is not None else None,
        "regular": bool(regular),
    }
