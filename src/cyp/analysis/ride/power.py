"""Power metrics: NP / IF / TSS / VI / kJ, power curve, time in power zones, W'bal minimum.

Definitions follow docs/glossary/coggan_np_if_tss.md, time_in_zone_tid.md and cp_wprime.md:

- ``NP = mean(P30s ** 4) ** 0.25`` over *moving* seconds (coasting 0 W counts; stationary and
  paused seconds do not). This is intervals.icu's basis and tracks its ``icu_weighted_avg_watts``
  within ~1 %; the classic all-recorded-seconds NP is kept as ``np_recorded_w`` for reference.
  ``None`` for rides under :data:`NP_MIN_DURATION_S`.
- ``IF = NP / FTP``; ``TSS = moving_s * NP * IF / (FTP * 3600) * 100`` (icu uses moving time).
- ``VI = NP / avg power``; ``kJ = sum(P) / 1000``.
- Power curve: best trailing mean at :data:`POWER_CURVE_DURATIONS` (+ W/kg).
- Time in zone: seconds of *moving* samples in each ``[lo, hi)`` band (0 W -> Z1), icu basis.
- W'bal: Skiba 2012 integral form, ``tau = 546 * exp(-0.01 * DCP) + 316``; minimum over the ride.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cyp.analysis.ride.frames import FloatArray, RideFrame, rolling_mean
from cyp.core.athlete import Zone, ZoneModel

POWER_CURVE_DURATIONS: tuple[int, ...] = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600, 5400)
NP_WINDOW_S = 30
#: NP/IF/TSS are not meaningful under 20 min (strava-analyis convention).
NP_MIN_DURATION_S = 1200

#: Coggan 7-zone upper bounds in % FTP (``None`` = open-ended Z7).
COGGAN_PCT_BOUNDS: tuple[float | None, ...] = (55, 75, 90, 105, 120, 150, None)
COGGAN_ZONE_NAMES: tuple[str, ...] = (
    "Active Recovery",
    "Endurance",
    "Tempo",
    "Threshold",
    "VO2 Max",
    "Anaerobic",
    "Neuromuscular",
)


# ------------------------------------------------------------------------------- series


def recorded_power(frame: RideFrame, watts: FloatArray | None = None) -> FloatArray | None:
    """Power over recorded seconds, NaN -> 0 W. ``watts`` overrides the frame (estimates)."""
    source = frame.watts if watts is None else watts
    if source is None:
        return None
    return np.nan_to_num(source[frame.recorded], nan=0.0)


def moving_power(frame: RideFrame, watts: FloatArray | None = None) -> FloatArray | None:
    """Power over moving seconds, NaN -> 0 W."""
    source = frame.watts if watts is None else watts
    if source is None:
        return None
    return np.nan_to_num(source[frame.moving], nan=0.0)


# ------------------------------------------------------------------------------- scalars


def normalized_power(p: FloatArray, *, min_duration_s: int = NP_MIN_DURATION_S) -> float | None:
    """Coggan NP of a 1 Hz NaN-free power series; ``None`` when too short."""
    if p.size < max(min_duration_s, NP_WINDOW_S):
        return None
    r30 = rolling_mean(p, NP_WINDOW_S)
    valid = r30[np.isfinite(r30)]
    if valid.size == 0:
        return None
    mean4 = float(np.mean(valid**4))
    if not math.isfinite(mean4) or mean4 < 0:
        return None
    return float(mean4**0.25)


def intensity_factor(np_w: float | None, ftp: float | None) -> float | None:
    """``IF = NP / FTP``."""
    if np_w is None or not ftp or ftp <= 0:
        return None
    return np_w / ftp


def training_stress_score(
    np_w: float | None, if_: float | None, moving_s: float | None, ftp: float | None
) -> float | None:
    """``TSS = moving_s * NP * IF / (FTP * 3600) * 100``."""
    if np_w is None or if_ is None or not ftp or ftp <= 0 or not moving_s or moving_s <= 0:
        return None
    return moving_s * np_w * if_ / (ftp * 3600.0) * 100.0


def variability_index(np_w: float | None, avg_w: float | None) -> float | None:
    """``VI = NP / average power``."""
    if np_w is None or not avg_w or avg_w <= 0:
        return None
    return np_w / avg_w


def work_kj(p: FloatArray) -> float:
    """Mechanical work in kJ of a 1 Hz power series."""
    return float(p.sum()) / 1000.0


def power_curve(
    p: FloatArray,
    durations: tuple[int, ...] = POWER_CURVE_DURATIONS,
    weight_kg: float | None = None,
) -> tuple[dict[int, float], dict[int, float]]:
    """Best trailing-mean power per duration (W) and the W/kg version (empty without weight)."""
    watts: dict[int, float] = {}
    for d in durations:
        if d > p.size or d <= 0:
            continue
        r = rolling_mean(p, d)
        best = float(np.nanmax(r))
        if math.isfinite(best):
            watts[d] = best
    wkg: dict[int, float] = {}
    if weight_kg and weight_kg > 0:
        wkg = {d: w / weight_kg for d, w in watts.items()}
    return watts, wkg


# ------------------------------------------------------------------------------- zones


def zones_from_pct_bounds(
    ftp: float, pct_bounds: list[float | None] | tuple[float | None, ...], names: Any = None
) -> ZoneModel | None:
    """Build a power ``ZoneModel`` from upper bounds in % FTP (icu ``icu_power_zones`` format).

    A bound ``>= 900`` or ``None`` marks the open-ended top zone.
    """
    if ftp <= 0 or not pct_bounds:
        return None
    labels = list(names) if isinstance(names, list | tuple) else list(COGGAN_ZONE_NAMES)
    zones: list[Zone] = []
    lo = 0.0
    for i, pct in enumerate(pct_bounds):
        if pct is None or pct >= 900:
            hi: float | None = None
        else:
            hi = round(ftp * float(pct) / 100.0, 1)
        name = str(labels[i]) if i < len(labels) else f"Z{i + 1}"
        zones.append(Zone(idx=i + 1, name=name, lo=lo, hi=hi))
        if hi is None:
            break
        lo = hi
    if not zones:
        return None
    return ZoneModel(kind="power", anchor=float(ftp), zones=zones)


def coggan_zones(ftp: float) -> ZoneModel | None:
    """Classic Coggan 7 zones for ``ftp``."""
    return zones_from_pct_bounds(ftp, COGGAN_PCT_BOUNDS)


def time_in_zones(values: FloatArray, zones: ZoneModel) -> dict[str, float]:
    """Seconds per zone (``{"Z1": s, ...}``) of a 1 Hz series; NaNs are ignored.

    Values below the first zone's ``lo`` are counted in the first zone, so 0 W lands in Z1.
    """
    v = values[np.isfinite(values)]
    out: dict[str, float] = {}
    first = zones.zones[0]
    for z in zones.zones:
        if z is first:
            mask = v < z.hi if z.hi is not None else np.ones(v.size, dtype=bool)
        else:
            mask = v >= z.lo
            if z.hi is not None:
                mask &= v < z.hi
        out[f"Z{z.idx}"] = float(np.count_nonzero(mask))
    return out


def tiz_three_zone(tiz: dict[str, float]) -> dict[str, float]:
    """Collapse 7 zones to low (Z1+Z2) / mid (Z3+Z4) / high (Z5+) seconds."""
    low = tiz.get("Z1", 0.0) + tiz.get("Z2", 0.0)
    mid = tiz.get("Z3", 0.0) + tiz.get("Z4", 0.0)
    high = sum(v for k, v in tiz.items() if k not in {"Z1", "Z2", "Z3", "Z4"})
    return {"low": low, "mid": mid, "high": high}


# ------------------------------------------------------------------------------- W'bal


def wbal_min(p: FloatArray, cp: float | None, w_prime: float | None) -> float | None:
    """Minimum W'bal (J) over the ride, Skiba 2012 integral form.

    ``W'bal(t) = W' - sum_{u<=t} max(P(u)-CP, 0) * exp(-(t-u)/tau)`` with
    ``tau = 546 * exp(-0.01 * DCP) + 316`` and ``DCP = CP - mean(P | P < CP)``.
    """
    if not cp or not w_prime or cp <= 0 or w_prime <= 0 or p.size == 0:
        return None
    below = p[p < cp]
    dcp = float(cp - below.mean()) if below.size else 0.0
    tau = 546.0 * math.exp(-0.01 * dcp) + 316.0
    decay = math.exp(-1.0 / tau)
    expended = np.clip(p - cp, 0.0, None)
    integral = 0.0
    peak = 0.0
    for e in expended:
        integral = integral * decay + float(e)
        if integral > peak:
            peak = integral
    return float(w_prime - peak)


# ------------------------------------------------------------------------------- bundle


@dataclass
class PowerMetrics:
    """Everything power-derived for one ride (``None``/empty when not computable)."""

    basis: str  # "power" | "estimated"
    np_w: float | None = None
    np_recorded_w: float | None = None
    if_: float | None = None
    tss: float | None = None
    vi: float | None = None
    avg_w: float | None = None
    max_w: float | None = None
    kj: float | None = None
    moving_s: int = 0
    recording_s: int = 0
    power_curve: dict[int, float] = field(default_factory=dict)
    power_curve_wkg: dict[int, float] = field(default_factory=dict)
    time_in_zone: dict[str, float] | None = None
    wbal_min_j: float | None = None
    ftp: float | None = None


def compute_power_metrics(
    frame: RideFrame,
    *,
    ftp: float | None,
    weight_kg: float | None,
    zones: ZoneModel | None,
    cp: float | None = None,
    w_prime: float | None = None,
    watts: FloatArray | None = None,
    basis: str = "power",
) -> PowerMetrics:
    """Compute :class:`PowerMetrics` from measured (default) or supplied ``watts``."""
    out = PowerMetrics(basis=basis, ftp=ftp, moving_s=frame.moving_s, recording_s=frame.recording_s)
    p = recorded_power(frame, watts)
    if p is None or p.size == 0:
        return out
    out.avg_w = float(p.mean())
    out.max_w = float(p.max())
    out.kj = work_kj(p)
    out.np_recorded_w = normalized_power(p)
    mp = moving_power(frame, watts)
    out.np_w = normalized_power(mp) if mp is not None else None
    out.if_ = intensity_factor(out.np_w, ftp)
    out.tss = training_stress_score(out.np_w, out.if_, frame.moving_s, ftp)
    out.vi = variability_index(out.np_w, out.avg_w)
    out.power_curve, out.power_curve_wkg = power_curve(p, weight_kg=weight_kg)
    if zones is not None and mp is not None:
        out.time_in_zone = time_in_zones(mp, zones)
    out.wbal_min_j = wbal_min(p, cp, w_prime)
    return out
