"""Durability: EF, aerobic decoupling, HR lag, HR time in zone and the RIDE.LOG status/next.

Definitions (docs/glossary/decoupling_hr_lag.md, efficiency_factor.md):

- **EF** = NP / average HR over moving seconds (W/bpm).
- **Decoupling (Pw:Hr)**: moving, pedalling (P > 0), HR > 0 seconds after the first
  ``Thresholds.decoupling_warmup_s`` are split at the temporal midpoint;
  ``EF_half = mean(P) / mean(HR)`` (Friel / icu definition, average power not NP so short
  halves are allowed) and ``decoupling % = (EF_first - EF_second) / EF_first * 100``.
  Positive = HR rose for the same power in the second half. The value is reported for every
  ride but flagged ``reliable`` only when the ride is long and steady enough
  (``decoupling_min_moving_s``, ``IF <= decoupling_max_if``, ``VI <= decoupling_max_vi``,
  measured power).
- **HR lag**: cross-correlation between 30 s-smoothed power and HR over recorded seconds,
  lag bounded to ``[lag_min_s, lag_max_s]``; the lag maximising the Pearson correlation is
  reported with its ``corr``. ``None`` when fewer than ``lag_min_samples`` or
  ``corr < lag_min_corr`` (free-form rides may have no usable response; missing != bad).
- **Status / next** mirror the fields the athlete reads in ``RIDE.LOG``; the exact rules and
  the numbers behind them live in :class:`Thresholds` and :func:`classify_status`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np

from cyp.analysis.ride.frames import NP_WINDOW_S, FloatArray, RideFrame, rolling_mean
from cyp.analysis.ride.power import time_in_zones
from cyp.core.athlete import ZoneModel
from cyp.core.load import ReadinessStatus, Recommendation

Status = ReadinessStatus
Next = Recommendation
Basis = Literal["power", "estimated", "speed"]


@dataclass(frozen=True)
class Thresholds:
    """Every number behind decoupling reliability, HR lag and the status/next verdict.

    Status rules (first match wins, evaluated in this order):

    - ``OVERREACHED``: reliable decoupling >= ``overreached_min_decoupling`` (10 %) **or**
      ``tss >= overreached_min_tss`` (250).
    - ``BLUNTED``: HR lag >= ``blunted_min_lag_s`` (90 s) — the autonomic response is slow.
    - ``FRESH``: ride moved >= ``fresh_min_moving_s`` (45 min), lag <= ``fresh_max_lag_s``
      (45 s) and reliable decoupling <= ``fresh_max_decoupling`` (2 %).
    - ``NORMAL`` otherwise (including when lag/decoupling are unavailable).

    Next rules:

    - ``OVERREACHED`` -> ``REST`` when ``tss >= rest_min_tss`` (300) else ``EASY``.
    - ``BLUNTED`` -> ``EASY``.
    - ``FRESH`` -> ``UPGRADE`` when ``tss <= upgrade_max_tss`` (80) else ``AS_PLANNED``.
    - ``NORMAL`` -> ``EASY`` when ``tss >= easy_min_tss`` (150) or reliable decoupling >=
      ``easy_min_decoupling`` (5 %), else ``AS_PLANNED``.
    """

    # decoupling
    decoupling_warmup_s: int = 600
    decoupling_min_half_s: int = 600
    decoupling_min_moving_s: int = 3600
    decoupling_max_if: float = 0.85
    # Outdoor stop-start rides sit at VI 1.3-1.6; above ~1.8 the ride is coasting-dominated.
    decoupling_max_vi: float = 1.6
    # HR lag
    lag_min_s: int = 0
    lag_max_s: int = 120
    lag_min_samples: int = 600
    lag_min_corr: float = 0.3
    # status
    fresh_max_decoupling: float = 2.0
    fresh_max_lag_s: float = 45.0
    fresh_min_moving_s: int = 2700
    blunted_min_lag_s: float = 90.0
    overreached_min_decoupling: float = 10.0
    overreached_min_tss: float = 250.0
    # next
    rest_min_tss: float = 300.0
    easy_min_tss: float = 150.0
    easy_min_decoupling: float = 5.0
    upgrade_max_tss: float = 80.0

    def as_dict(self) -> dict[str, Any]:
        """Plain dict (for persisting the thresholds used next to the result)."""
        return asdict(self)


DEFAULT_THRESHOLDS = Thresholds()


# ------------------------------------------------------------------------------- EF


def efficiency_factor(np_w: float | None, avg_hr: float | None) -> float | None:
    """``EF = NP / avg HR``."""
    if np_w is None or not avg_hr or avg_hr <= 0:
        return None
    return np_w / avg_hr


def average_hr(frame: RideFrame) -> float | None:
    """Mean HR over moving seconds with HR > 0."""
    if frame.hr is None:
        return None
    hr = frame.hr[frame.moving]
    hr = hr[np.isfinite(hr) & (hr > 0)]
    return float(hr.mean()) if hr.size else None


def max_hr(frame: RideFrame) -> float | None:
    """Maximum recorded HR."""
    if frame.hr is None:
        return None
    hr = frame.hr[np.isfinite(frame.hr)]
    return float(hr.max()) if hr.size else None


# ------------------------------------------------------------------------------- decoupling


@dataclass
class Decoupling:
    """Pw:Hr decoupling with the half-ride numbers it came from."""

    pct: float
    basis: Basis
    ef_first: float
    ef_second: float
    power_first: float
    power_second: float
    hr_first: float
    hr_second: float
    n_first_s: int
    n_second_s: int
    excluded_warmup_s: int
    reliable: bool
    unreliable_reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict for ``activity_metrics.hr_drift_detail``."""
        return {
            "decoupling_pct": round(self.pct, 2),
            "basis": self.basis,
            "ef_first": round(self.ef_first, 4),
            "ef_second": round(self.ef_second, 4),
            "power_first_w": round(self.power_first, 1),
            "power_second_w": round(self.power_second, 1),
            "hr_first_bpm": round(self.hr_first, 1),
            "hr_second_bpm": round(self.hr_second, 1),
            "n_first_s": self.n_first_s,
            "n_second_s": self.n_second_s,
            "excluded_warmup_s": self.excluded_warmup_s,
            "reliable": self.reliable,
            "unreliable_reason": self.unreliable_reason,
        }


def decoupling(
    frame: RideFrame,
    output: FloatArray | None,
    *,
    basis: Basis,
    if_: float | None,
    vi: float | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> Decoupling | None:
    """Aerobic decoupling of ``output`` (power W or speed m/s) vs HR; see module docstring."""
    if frame.hr is None or output is None:
        return None
    hr = frame.hr
    mask = frame.moving & np.isfinite(hr) & (hr > 0) & np.isfinite(output) & (output > 0)
    t0 = int(frame.t_s[0])
    warm = thresholds.decoupling_warmup_s
    after_warm = mask & (frame.t_s - t0 >= warm)
    excluded = warm
    if after_warm.sum() < 2 * thresholds.decoupling_min_half_s:
        after_warm = mask  # too short to drop a warm-up; use everything
        excluded = 0
    idx = np.flatnonzero(after_warm)
    if idx.size < 2 * thresholds.decoupling_min_half_s and idx.size < 4:
        return None
    t = frame.t_s[idx]
    mid = t[0] + (t[-1] - t[0]) / 2.0
    split = int(np.searchsorted(t, mid, side="right"))
    split = min(max(split, 1), idx.size - 1)
    first, second = idx[:split], idx[split:]
    p1, p2 = float(output[first].mean()), float(output[second].mean())
    h1, h2 = float(hr[first].mean()), float(hr[second].mean())
    if h1 <= 0 or h2 <= 0 or p1 <= 0:
        return None
    ef1, ef2 = p1 / h1, p2 / h2
    pct = (ef1 - ef2) / ef1 * 100.0
    reason: str | None = None
    if basis != "power":
        reason = f"basis={basis}"
    elif frame.moving_s < thresholds.decoupling_min_moving_s:
        reason = f"moving_s<{thresholds.decoupling_min_moving_s}"
    elif if_ is not None and if_ > thresholds.decoupling_max_if:
        reason = f"IF>{thresholds.decoupling_max_if}"
    elif vi is not None and vi > thresholds.decoupling_max_vi:
        reason = f"VI>{thresholds.decoupling_max_vi}"
    elif first.size < thresholds.decoupling_min_half_s:
        reason = f"half<{thresholds.decoupling_min_half_s}s"
    return Decoupling(
        pct=pct,
        basis=basis,
        ef_first=ef1,
        ef_second=ef2,
        power_first=p1,
        power_second=p2,
        hr_first=h1,
        hr_second=h2,
        n_first_s=int(first.size),
        n_second_s=int(second.size),
        excluded_warmup_s=excluded,
        reliable=reason is None,
        unreliable_reason=reason,
    )


# ------------------------------------------------------------------------------- HR lag


@dataclass
class HrLag:
    """Cross-correlation lag between power and HR."""

    lag_s: int
    corr: float
    n_samples: int

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict."""
        return {"lag_s": self.lag_s, "corr": round(self.corr, 3), "n_samples": self.n_samples}


def hr_lag(
    frame: RideFrame,
    watts: FloatArray | None = None,
    *,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> HrLag | None:
    """Lag (s) by which HR trails 30 s-smoothed power, bounded to ``[lag_min_s, lag_max_s]``.

    Both series are smoothed with the same trailing 30 s window over the full grid (pauses as
    0 W / forward-filled HR), restricted to recorded seconds with HR, then demeaned. For each
    candidate lag the Pearson correlation of ``power[:-lag]`` with ``hr[lag:]`` is computed;
    the best lag is returned if its correlation reaches ``lag_min_corr``.
    """
    source = frame.watts if watts is None else watts
    if source is None or frame.hr is None:
        return None
    p30 = frame.power_30s(source)
    if p30 is None:
        return None
    hr = frame.hr.copy()
    valid = frame.recorded & np.isfinite(hr) & (hr > 0) & np.isfinite(p30)
    if int(valid.sum()) < thresholds.lag_min_samples:
        return None
    hr_filled = hr.copy()
    hr_filled[~np.isfinite(hr_filled)] = 0.0
    hr30 = rolling_mean(hr_filled, NP_WINDOW_S)
    p = p30[valid]
    h = hr30[valid]
    keep = np.isfinite(h)
    p, h = p[keep], h[keep]
    n = p.size
    if n < thresholds.lag_min_samples:
        return None
    p = p - p.mean()
    h = h - h.mean()
    if p.std() == 0 or h.std() == 0:
        return None
    best_lag, best_corr = 0, -2.0
    for lag in range(thresholds.lag_min_s, thresholds.lag_max_s + 1):
        if lag >= n - 2:
            break
        a = p[: n - lag] if lag else p
        b = h[lag:] if lag else h
        denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
        if denom == 0:
            continue
        corr = float((a * b).sum() / denom)
        if corr > best_corr:
            best_corr, best_lag = corr, lag
    if best_corr < thresholds.lag_min_corr:
        return None
    return HrLag(lag_s=best_lag, corr=best_corr, n_samples=n)


# ------------------------------------------------------------------------------- HR TIZ


def hr_time_in_zones(frame: RideFrame, zones: ZoneModel | None) -> dict[str, float] | None:
    """Seconds per HR zone over moving samples."""
    if zones is None or frame.hr is None:
        return None
    hr = frame.hr[frame.moving]
    hr = hr[np.isfinite(hr) & (hr > 0)]
    if hr.size == 0:
        return None
    return time_in_zones(hr, zones)


# ------------------------------------------------------------------------------- status


def classify_status(
    *,
    decoupling_result: Decoupling | None,
    lag: HrLag | None,
    tss: float | None,
    moving_s: int,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> tuple[Status, Next]:
    """RIDE.LOG ``STATUS`` / ``NEXT`` from decoupling, HR lag and TSS (see :class:`Thresholds`)."""
    dec = (
        decoupling_result.pct
        if decoupling_result is not None and decoupling_result.reliable
        else None
    )
    lag_s = float(lag.lag_s) if lag is not None else None
    status: Status = "NORMAL"
    if (dec is not None and dec >= thresholds.overreached_min_decoupling) or (
        tss is not None and tss >= thresholds.overreached_min_tss
    ):
        status = "OVERREACHED"
    elif lag_s is not None and lag_s >= thresholds.blunted_min_lag_s:
        status = "BLUNTED"
    elif (
        moving_s >= thresholds.fresh_min_moving_s
        and lag_s is not None
        and lag_s <= thresholds.fresh_max_lag_s
        and dec is not None
        and dec <= thresholds.fresh_max_decoupling
    ):
        status = "FRESH"

    nxt: Next
    if status == "OVERREACHED":
        nxt = "REST" if tss is not None and tss >= thresholds.rest_min_tss else "EASY"
    elif status == "BLUNTED":
        nxt = "EASY"
    elif status == "FRESH":
        nxt = "UPGRADE" if tss is not None and tss <= thresholds.upgrade_max_tss else "AS_PLANNED"
    elif (tss is not None and tss >= thresholds.easy_min_tss) or (
        dec is not None and dec >= thresholds.easy_min_decoupling
    ):
        nxt = "EASY"
    else:
        nxt = "AS_PLANNED"
    return status, nxt
