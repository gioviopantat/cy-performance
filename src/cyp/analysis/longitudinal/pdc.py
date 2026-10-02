"""Power-duration model: CP/W′ 2-parameter and 3-parameter fits + FTP-change proposals.

Pure functions (docs/04 §3, docs/glossary/cp_wprime.md, eftp.md):

- :func:`mmp_from_rides` — mean-maximal power per duration over a window of rides' power curves.
- :func:`fit_cp_2p` — linear work-time model ``W = CP·t + W′`` on efforts of 3–20 min
  (Monod & Scherrer); least squares, ``r²`` reported.
- :func:`fit_cp_3p` — Morton's 3-parameter hyperbola ``P(t) = CP + W′ / (t + W′/(Pmax − CP))``
  on 5 s – 30 min, non-linear least squares (scipy).
- :func:`compare_with_icu` — our fit vs icu's ``mmp-model`` (CP, W′, Pmax, eFTP).
- :func:`ftp_proposal` — flags "FTP looks different from your setting" when a daily estimate
  series sits ≥ 3 % away from the configured FTP, on the same side, for ≥ 14 consecutive days.
  It **never** changes FTP: the result is a proposal with an Explanation, applied only by the
  athlete (in icu) or by an explicit future ``cyp ftp accept`` command.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from scipy.optimize import curve_fit  # type: ignore[import-untyped]

from cyp.core.explain import Explanation, MethodRef, Reason

PDC_VERSION = "pdc_v1"
FIT_2P_RANGE_S = (180, 1200)
FIT_3P_RANGE_S = (5, 1800)
#: CP→FTP factor for the CP-derived estimate. CP from 3–20 min efforts sits a few % above a
#: 1-hour power; icu's own MS_2P model reports ftp ≈ 0.97–0.99 × CP.
CP_TO_FTP = 0.97
PROPOSAL_THRESHOLD = 0.03
PROPOSAL_MIN_DAYS = 14


@dataclass(frozen=True)
class CPFit:
    """Result of a CP model fit."""

    model: Literal["cp_2p", "cp_3p"]
    cp: float
    w_prime: float
    p_max: float | None
    r2: float | None
    points: list[tuple[int, float]]
    rmse_w: float | None = None

    @property
    def eftp(self) -> float:
        """FTP estimate derived from CP (:data:`CP_TO_FTP`)."""
        return self.cp * CP_TO_FTP


def mmp_from_rides(curves: Iterable[Mapping[str, Any] | None]) -> dict[int, float]:
    """Best power per duration across ``curves``.

    Each curve is ``{"300": watts, ...}`` or the ``activity_metrics.power_curve`` shape
    ``{"watts": {"300": ...}, "w_kg": {...}}``.
    """
    best: dict[int, float] = {}
    for curve in curves:
        if not curve:
            continue
        if isinstance(curve.get("watts"), dict):  # persisted shape {"watts": {...}, "w_kg": ...}
            curve = curve["watts"]
        for key, val in curve.items():
            try:
                d, w = int(key), float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(w) or w <= 0:
                continue
            if w > best.get(d, 0.0):
                best[d] = w
    return dict(sorted(best.items()))


def _in_range(mmp: Mapping[int, float], lo: int, hi: int) -> list[tuple[int, float]]:
    return [(d, w) for d, w in sorted(mmp.items()) if lo <= d <= hi and w > 0]


def fit_cp_2p(mmp: Mapping[int, float], rng: tuple[int, int] = FIT_2P_RANGE_S) -> CPFit | None:
    """Linear work-time CP fit; ``None`` with fewer than 2 points or a non-physical result."""
    pts = _in_range(mmp, *rng)
    if len(pts) < 2:
        return None
    t = np.array([p[0] for p in pts], dtype=float)
    work = np.array([p[0] * p[1] for p in pts], dtype=float)
    slope, intercept = np.polyfit(t, work, 1)
    if slope <= 0 or intercept <= 0:
        return None
    pred = slope * t + intercept
    ss_res = float(np.sum((work - pred) ** 2))
    ss_tot = float(np.sum((work - work.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    p_pred = slope + intercept / t
    rmse = float(np.sqrt(np.mean((p_pred - np.array([p[1] for p in pts])) ** 2)))
    return CPFit("cp_2p", float(slope), float(intercept), None, r2, pts, rmse)


def _morton(t: Any, cp: float, w_prime: float, p_max: float) -> Any:
    return cp + w_prime / (t + w_prime / (p_max - cp))


def fit_cp_3p(mmp: Mapping[int, float], rng: tuple[int, int] = FIT_3P_RANGE_S) -> CPFit | None:
    """Morton 3-parameter fit; ``None`` with fewer than 4 points or on non-convergence."""
    pts = _in_range(mmp, *rng)
    if len(pts) < 4:
        return None
    t = np.array([p[0] for p in pts], dtype=float)
    p = np.array([p[1] for p in pts], dtype=float)
    seed = fit_cp_2p(mmp) or CPFit("cp_2p", float(p.min()), 15000.0, None, None, [])
    p0 = [seed.cp, seed.w_prime, max(float(p.max()) * 1.1, seed.cp + 50)]
    try:
        popt, _ = curve_fit(
            _morton,
            t,
            p,
            p0=p0,
            bounds=([50.0, 1000.0, 100.0], [800.0, 100000.0, 3000.0]),
            maxfev=10000,
        )
    except (RuntimeError, ValueError):
        return None
    cp, w_prime, p_max = (float(x) for x in popt)
    if p_max <= cp:
        return None
    pred = _morton(t, cp, w_prime, p_max)
    ss_res = float(np.sum((p - pred) ** 2))
    ss_tot = float(np.sum((p - p.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    rmse = float(np.sqrt(ss_res / len(p)))
    return CPFit("cp_3p", cp, w_prime, p_max, r2, pts, rmse)


@dataclass
class IcuModelComparison:
    """Our fit vs icu's ``mmp-model``."""

    ours: CPFit
    icu_cp: float | None
    icu_w_prime: float | None
    icu_p_max: float | None
    icu_eftp: float | None

    @property
    def cp_diff_pct(self) -> float | None:
        """(ours − icu) / icu CP in %."""
        if not self.icu_cp:
            return None
        return (self.ours.cp - self.icu_cp) / self.icu_cp * 100.0

    @property
    def w_prime_diff_pct(self) -> float | None:
        """(ours − icu) / icu W′ in %."""
        if not self.icu_w_prime:
            return None
        return (self.ours.w_prime - self.icu_w_prime) / self.icu_w_prime * 100.0


def compare_with_icu(ours: CPFit, icu_model: Mapping[str, Any] | None) -> IcuModelComparison:
    """Pair our fit with icu's model dict (``criticalPower``, ``wPrime``, ``pMax``, ``ftp``)."""
    m = icu_model or {}

    def num(key: str) -> float | None:
        v = m.get(key)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None

    return IcuModelComparison(
        ours=ours,
        icu_cp=num("criticalPower") or num("cp"),
        icu_w_prime=num("wPrime") or num("w_prime"),
        icu_p_max=num("pMax") or num("p_max"),
        icu_eftp=num("ftp") or num("eftp_icu"),
    )


def explain_fit(
    fit: CPFit, comparison: IcuModelComparison | None, *, as_of: dt.date
) -> Explanation:
    """Explanation for a CP fit (key ``pdc.<model>.<date>``)."""
    because = [
        Reason(
            text_zh=(
                f"用 {len(fit.points)} 個最佳功率點"
                f"（{fit.points[0][0]}–{fit.points[-1][0]} 秒）擬合，"
                f"CP {fit.cp:.0f} W、W′ {fit.w_prime / 1000:.1f} kJ"
                + (f"、Pmax {fit.p_max:.0f} W" if fit.p_max else "")
            ),
            evidence={
                "cp": round(fit.cp, 1),
                "w_prime": round(fit.w_prime),
                "p_max": round(fit.p_max) if fit.p_max else None,
                "points": [[d, round(w, 1)] for d, w in fit.points],
            },
        ),
    ]
    if fit.r2 is not None:
        because.append(
            Reason(
                text_zh=f"擬合度 r² {fit.r2:.3f}"
                + (f"、殘差 {fit.rmse_w:.1f} W" if fit.rmse_w else ""),
                evidence={"r2": round(fit.r2, 4), "rmse_w": round(fit.rmse_w or 0, 2)},
            )
        )
    if comparison is not None and comparison.icu_cp:
        because.append(
            Reason(
                text_zh=(
                    f"intervals.icu 模型 CP {comparison.icu_cp:.0f} W，差 "
                    f"{comparison.cp_diff_pct:+.1f} %"
                ),
                evidence={
                    "icu_cp": comparison.icu_cp,
                    "icu_w_prime": comparison.icu_w_prime,
                    "icu_eftp": comparison.icu_eftp,
                    "cp_diff_pct": round(comparison.cp_diff_pct or 0, 2),
                },
            )
        )
    good = fit.r2 is not None and fit.r2 > 0.98 and len(fit.points) >= 3
    return Explanation(
        key=f"pdc.{fit.model}.{as_of.isoformat()}",
        headline_zh=f"臨界功率 CP 約 {fit.cp:.0f} W（推估 FTP ≈ {fit.eftp:.0f} W）",
        because=because,
        method=MethodRef(
            model_id=fit.model,
            version=PDC_VERSION,
            inputs={"range_s": [fit.points[0][0], fit.points[-1][0]], "cp_to_ftp": CP_TO_FTP},
            doc="docs/glossary/cp_wprime.md",
        ),
        confidence="high" if good else "medium" if len(fit.points) >= 3 else "low",
        glossary_terms=["cp_wprime", "eftp"],
    )


# ------------------------------------------------------------------------- FTP proposals


@dataclass
class FtpProposal:
    """A *suggested* FTP change. Never applied automatically."""

    current_ftp: float
    proposed_ftp: float | None
    direction: Literal["up", "down", "none"]
    days_sustained: int
    median_estimate: float | None
    window: tuple[dt.date, dt.date] | None
    sources: list[str] = field(default_factory=list)
    best_20min_w: float | None = None
    unsupported: bool = False
    explanation: Explanation | None = None

    @property
    def change_pct(self) -> float | None:
        """Proposed change in % of the current FTP."""
        if self.proposed_ftp is None:
            return None
        return (self.proposed_ftp - self.current_ftp) / self.current_ftp * 100.0


def ftp_proposal(
    current_ftp: float,
    estimates: Mapping[dt.date, float],
    *,
    as_of: dt.date,
    threshold: float = PROPOSAL_THRESHOLD,
    min_days: int = PROPOSAL_MIN_DAYS,
    sources: Sequence[str] = (),
    best_20min_w: float | None = None,
) -> FtpProposal:
    """Propose an FTP change when daily estimates sit ≥ ``threshold`` away for ``min_days``.

    ``estimates`` is a daily series (e.g. icu eFTP per wellness day, forward-filled). The run of
    consecutive days ending at ``as_of`` that are all ≥ ``threshold`` above (or all below) the
    current FTP must be at least ``min_days`` long. The proposal is the run's median, rounded
    to 1 W. An *upward* proposal also needs a supporting effort (glossary eftp.md): when
    ``best_20min_w`` (recent best 20 min) is given, ``0.95 × best_20min_w`` must reach the
    proposal minus ``threshold``; otherwise the proposal is withheld (``unsupported=True``).
    """
    if current_ftp <= 0:
        raise ValueError("current_ftp must be positive")
    run: list[float] = []
    direction: Literal["up", "down", "none"] = "none"
    day = as_of
    while day in estimates:
        rel = (estimates[day] - current_ftp) / current_ftp
        side: Literal["up", "down", "none"] = (
            "up" if rel >= threshold else "down" if rel <= -threshold else "none"
        )
        if side == "none" or (direction != "none" and side != direction):
            break
        direction = side
        run.append(estimates[day])
        day -= dt.timedelta(days=1)
    n = len(run)
    median = statistics.median(run) if run else None
    proposed = float(round(median)) if (median is not None and n >= min_days) else None
    unsupported = False
    if (
        proposed is not None
        and direction == "up"
        and best_20min_w is not None
        and 0.95 * best_20min_w < proposed * (1 - threshold)
    ):
        proposed, unsupported = None, True
    window = (as_of - dt.timedelta(days=n - 1), as_of) if n else None
    prop = FtpProposal(
        current_ftp=current_ftp,
        proposed_ftp=proposed,
        direction=direction if proposed is not None else "none",
        days_sustained=n,
        median_estimate=median,
        window=window,
        sources=list(sources),
        best_20min_w=best_20min_w,
        unsupported=unsupported,
    )
    prop.explanation = _explain_proposal(prop, as_of, threshold, min_days, estimates)
    return prop


def _explain_proposal(
    prop: FtpProposal,
    as_of: dt.date,
    threshold: float,
    min_days: int,
    estimates: Mapping[dt.date, float],
) -> Explanation:
    latest = estimates.get(as_of)
    because: list[Reason] = []
    if latest is not None:
        because.append(
            Reason(
                text_zh=(
                    f"目前設定 FTP {prop.current_ftp:.0f} W，最新估計 {latest:.0f} W"
                    f"（{(latest - prop.current_ftp) / prop.current_ftp * 100:+.1f} %）"
                ),
                evidence={"current_ftp": prop.current_ftp, "latest_estimate": latest},
            )
        )
    because.append(
        Reason(
            text_zh=(
                f"估計值連續 {prop.days_sustained} 天偏離 ≥ {threshold * 100:.0f} %"
                f"（門檻 {min_days} 天）"
            ),
            evidence={
                "days_sustained": prop.days_sustained,
                "min_days": min_days,
                "threshold_pct": threshold * 100,
                "median_estimate": round(prop.median_estimate, 1)
                if prop.median_estimate is not None
                else None,
            },
        )
    )
    if prop.best_20min_w is not None:
        because.append(
            Reason(
                text_zh=(
                    f"近期最佳 20 分鐘 {prop.best_20min_w:.0f} W × 0.95 = "
                    f"{prop.best_20min_w * 0.95:.0f} W"
                    + ("，撐不起上調，先不提案" if prop.unsupported else "")
                ),
                evidence={"best_20min_w": prop.best_20min_w, "unsupported": prop.unsupported},
            )
        )
    if prop.proposed_ftp is not None:
        headline = (
            f"建議把 FTP 從 {prop.current_ftp:.0f} W 調為 {prop.proposed_ftp:.0f} W"
            f"（{prop.change_pct:+.1f} %）— 需你確認，系統不會自動改"
        )
    else:
        headline = f"FTP {prop.current_ftp:.0f} W 維持不變（證據不足以提案）"
    return Explanation(
        key="ftp.proposal",
        headline_zh=headline,
        because=because,
        method=MethodRef(
            model_id="ftp_proposal",
            version=PDC_VERSION,
            inputs={"threshold": threshold, "min_days": min_days, "sources": prop.sources},
            doc="docs/glossary/eftp.md",
        ),
        confidence="high" if prop.days_sustained >= 2 * min_days else "medium",
        glossary_terms=["eftp", "cp_wprime"],
    )


def daily_series(
    points: Mapping[dt.date, float], start: dt.date, end: dt.date
) -> dict[dt.date, float]:
    """Forward-fill sparse dated values into a daily series over ``[start, end]``."""
    out: dict[dt.date, float] = {}
    last: float | None = None
    for d in sorted(points):
        if d < start:
            last = points[d]
    day = start
    while day <= end:
        if day in points:
            last = points[day]
        if last is not None:
            out[day] = last
        day += dt.timedelta(days=1)
    return out
