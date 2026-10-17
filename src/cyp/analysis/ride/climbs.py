"""Climb detection with VAM, W/kg, grade bands and a repeat-matching fingerprint.

Port of strava-analyis ``segments.detect_climbs``.

Algorithm: walk the *smoothed* altitude profile, greedily extending a run while altitude trends
up and tolerating drops of up to ``max(CLIMB_MAX_DROP_M, CLIMB_MAX_DROP_FRACTION * gain)``.
When the tolerance is exceeded the run is closed at its highest point, anchored at the lowest
point before the peak, and accepted when it is at least ``CLIMB_MIN_DISTANCE_M`` long and either
*steep* (gain >= 30 m and grade >= 3 %) or a *grind* (gain >= 100 m and grade >= 1.5 %).

Fingerprint: ``"{lat:.3f},{lng:.3f}|L{bucket}"`` — start position rounded to ~100 m plus the
climb length bucketed to 500 m — stable enough to find repeats of the same road.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from cyp.analysis.ride.frames import FloatArray, RideFrame, fill_gaps

CLIMB_MIN_DISTANCE_M = 500.0
CLIMB_MIN_GAIN_M = 30.0
CLIMB_MIN_AVG_GRADE = 3.0
CLIMB_MAJOR_GAIN_M = 100.0
CLIMB_MIN_GRIND_GRADE = 1.5
CLIMB_MAX_DROP_FRACTION = 0.20
CLIMB_MAX_DROP_M = 10.0
#: Grade bands (%) for ``grade_bands`` seconds: [<3, 3-6, 6-9, 9-12, >=12].
GRADE_BAND_EDGES: tuple[float, ...] = (3.0, 6.0, 9.0, 12.0)
GRADE_BAND_LABELS: tuple[str, ...] = ("lt3", "3_6", "6_9", "9_12", "ge12")
#: Window (s) for the max-grade estimate from smoothed altitude / distance.
MAX_GRADE_WINDOW_S = 30
FINGERPRINT_LENGTH_BUCKET_M = 500.0


def _nanmean(x: FloatArray | None, lo: int, hi: int) -> float | None:
    if x is None:
        return None
    seg = x[lo : hi + 1]
    seg = seg[np.isfinite(seg)]
    return float(seg.mean()) if seg.size else None


def _nanmax(x: FloatArray | None, lo: int, hi: int) -> float | None:
    if x is None:
        return None
    seg = x[lo : hi + 1]
    seg = seg[np.isfinite(seg)]
    return float(seg.max()) if seg.size else None


def grade_bands(grade: FloatArray) -> dict[str, float]:
    """Seconds per grade band of a 1 Hz grade series (NaNs ignored)."""
    g = grade[np.isfinite(grade)]
    edges = (-np.inf, *GRADE_BAND_EDGES, np.inf)
    out: dict[str, float] = {}
    for i, label in enumerate(GRADE_BAND_LABELS):
        out[label] = float(np.count_nonzero((g >= edges[i]) & (g < edges[i + 1])))
    return out


def fingerprint(lat: float | None, lng: float | None, distance_m: float | None) -> str | None:
    """Repeat-matching key from rounded start position and a length bucket."""
    if lat is None or lng is None or distance_m is None or not np.isfinite([lat, lng]).all():
        return None
    bucket = int(distance_m // FINGERPRINT_LENGTH_BUCKET_M)
    return f"{lat:.3f},{lng:.3f}|L{bucket}"


def climb_metrics(
    frame: RideFrame,
    start: int,
    end: int,
    *,
    alt_smoothed: FloatArray,
    dist: FloatArray,
    weight_kg: float | None,
    watts: FloatArray | None = None,
) -> dict[str, Any]:
    """Per-climb metrics for grid rows ``[start, end]`` (end = peak)."""
    duration = int(frame.t_s[end] - frame.t_s[start])
    moving_s = int(frame.moving[start : end + 1].sum())
    distance = float(dist[end] - dist[start])
    gain = max(0.0, float(alt_smoothed[end] - alt_smoothed[start]))
    avg_grade = gain / distance * 100.0 if distance > 0 else 0.0

    # Max grade over a 30 s window from smoothed altitude vs distance (robust to spikes).
    seg_alt = alt_smoothed[start : end + 1]
    seg_dist = dist[start : end + 1]
    max_grade: float | None = None
    w = min(MAX_GRADE_WINDOW_S, seg_alt.size - 1)
    if w >= 5:
        d_alt = seg_alt[w:] - seg_alt[:-w]
        d_dist = seg_dist[w:] - seg_dist[:-w]
        ok = d_dist > 1.0
        if ok.any():
            max_grade = float(np.max(d_alt[ok] / d_dist[ok] * 100.0))

    power = frame.watts if watts is None else watts
    avg_w = _nanmean(power, start, end)
    avg_hr = _nanmean(frame.hr, start, end)
    hr_max = _nanmax(frame.hr, start, end)
    avg_speed = distance / duration if duration > 0 else None
    vam = gain / (duration / 3600.0) if duration > 0 else None
    w_kg = avg_w / weight_kg if (avg_w is not None and weight_kg and weight_kg > 0) else None

    grade_series = frame.grade
    if grade_series is not None and np.isfinite(grade_series[start : end + 1]).any():
        bands = grade_bands(grade_series[start : end + 1])
    else:
        # Derive a 10 s grade from smoothed altitude when no grade stream exists.
        step = min(10, seg_alt.size - 1)
        if step >= 1:
            da = seg_alt[step:] - seg_alt[:-step]
            dd = seg_dist[step:] - seg_dist[:-step]
            with np.errstate(divide="ignore", invalid="ignore"):
                derived = np.where(dd > 0.5, da / dd * 100.0, np.nan)
            bands = grade_bands(np.asarray(derived, dtype=np.float64))
        else:
            bands = grade_bands(np.empty(0))

    lat0 = float(frame.lat[start]) if frame.lat is not None else None
    lng0 = float(frame.lng[start]) if frame.lng is not None else None
    if lat0 is not None and not np.isfinite(lat0):
        lat0 = None
    if lng0 is not None and not np.isfinite(lng0):
        lng0 = None

    return {
        "kind": "climb",
        "start_idx": int(start),
        "end_idx": int(end),
        "start_s": int(frame.t_s[start]),
        "duration_s": duration,
        "moving_s": moving_s,
        "distance_m": round(distance, 1),
        "gain_m": round(gain, 1),
        "avg_grade_pct": round(avg_grade, 2),
        "max_grade_pct": round(max_grade, 2) if max_grade is not None else None,
        "vam_m_h": round(vam, 1) if vam is not None else None,
        "avg_w": round(avg_w, 1) if avg_w is not None else None,
        "w_kg": round(w_kg, 2) if w_kg is not None else None,
        "avg_hr": round(avg_hr, 1) if avg_hr is not None else None,
        "max_hr": hr_max,
        "avg_speed_mps": round(avg_speed, 2) if avg_speed is not None else None,
        "grade_bands_s": bands,
        "start_lat": round(lat0, 5) if lat0 is not None else None,
        "start_lng": round(lng0, 5) if lng0 is not None else None,
        "fingerprint": fingerprint(lat0, lng0, distance),
    }


def detect_climbs(
    frame: RideFrame,
    *,
    weight_kg: float | None = None,
    watts: FloatArray | None = None,
) -> list[dict[str, Any]]:
    """Detect sustained climbs from smoothed altitude + distance (see module docstring)."""
    alt_s = frame.smoothed_altitude()
    if alt_s is None or frame.n < 2:
        return []
    if frame.dist is not None and np.isfinite(frame.dist).any():
        dist = fill_gaps(frame.dist)
    else:
        dist = np.arange(frame.n, dtype=np.float64)

    n = frame.n
    climbs: list[dict[str, Any]] = []
    i = 0
    while i < n - 1:
        if alt_s[i + 1] <= alt_s[i]:
            i += 1
            continue
        start = i
        peak_idx = i
        peak_alt = alt_s[i]
        j = i + 1
        while j < n:
            if alt_s[j] > peak_alt:
                peak_alt = alt_s[j]
                peak_idx = j
            else:
                gain_so_far = peak_alt - alt_s[start]
                drop = peak_alt - alt_s[j]
                if drop > max(CLIMB_MAX_DROP_M, CLIMB_MAX_DROP_FRACTION * gain_so_far):
                    break
            j += 1
        if peak_idx > start:
            start = start + int(np.argmin(alt_s[start : peak_idx + 1]))
        seg_dist = float(dist[peak_idx] - dist[start])
        seg_gain = float(peak_alt - alt_s[start])
        seg_grade = seg_gain / seg_dist * 100.0 if seg_dist > 0 else 0.0
        long_enough = seg_dist >= CLIMB_MIN_DISTANCE_M
        steep = seg_gain >= CLIMB_MIN_GAIN_M and seg_grade >= CLIMB_MIN_AVG_GRADE
        grind = seg_gain >= CLIMB_MAJOR_GAIN_M and seg_grade >= CLIMB_MIN_GRIND_GRADE
        if long_enough and (steep or grind):
            climbs.append(
                climb_metrics(
                    frame,
                    start,
                    peak_idx,
                    alt_smoothed=alt_s,
                    dist=dist,
                    weight_kg=weight_kg,
                    watts=watts,
                )
            )
            i = peak_idx + 1
        else:
            i = max(peak_idx, start + 1)
    return climbs
