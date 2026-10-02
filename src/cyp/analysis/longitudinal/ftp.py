"""FTP status: everything about "what is my FTP now?" in one fast, pure computation.

Inputs come from a :class:`~cyp.dataset.Dataset`; nothing here touches the DB or Parquet, so a
recompute costs a few milliseconds and can run on every UI interaction.

- Current FTP: the settings row effective on ``as_of`` (icu sport settings or a manual accept).
- MMP for the 42 / 90-day windows from per-ride power curves (measured power only).
- CP/W′ 2p + 3p fits per window, compared with icu's ``mmp-model``.
- Daily FTP-estimate series: icu eFTP from wellness when present, else our rolling 42-day
  CP 2p × :data:`~cyp.analysis.longitudinal.pdc.CP_TO_FTP` (vectorised over a rides × durations
  matrix).
- Proposal (:func:`~cyp.analysis.longitudinal.pdc.ftp_proposal`), never applied automatically.
- ``overrides``: what-if inputs (e.g. ``{"ftp": 270}`` to see the proposal against another
  setting) without touching stored data.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cyp.analysis.longitudinal import pdc
from cyp.core.explain import Explanation
from cyp.dataset import Dataset

WINDOWS: dict[str, int] = {"42d": 42, "90d": 90}
SERIES_DAYS = 56
ROLLING_WINDOW = 42


@dataclass
class WindowFit:
    """MMP + fits of one window."""

    window: str
    mmp: dict[int, float]
    cp_2p: pdc.CPFit | None
    cp_3p: pdc.CPFit | None
    icu: dict[str, float | None] | None
    explanations: list[Explanation] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """JSON-safe summary."""

        def fit(f: pdc.CPFit | None) -> dict[str, Any] | None:
            if f is None:
                return None
            cmp = pdc.compare_with_icu(f, _icu_model(self.icu))
            return {
                "cp": round(f.cp, 1),
                "w_prime": round(f.w_prime),
                "p_max": round(f.p_max) if f.p_max else None,
                "r2": round(f.r2, 4) if f.r2 is not None else None,
                "rmse_w": round(f.rmse_w, 2) if f.rmse_w is not None else None,
                "eftp": round(f.eftp, 1),
                "points": [[d, round(w, 1)] for d, w in f.points],
                "cp_diff_vs_icu_pct": round(cmp.cp_diff_pct, 2)
                if cmp.cp_diff_pct is not None
                else None,
            }

        return {
            "window": self.window,
            "mmp": {str(k): round(v, 1) for k, v in self.mmp.items()},
            "cp_2p": fit(self.cp_2p),
            "cp_3p": fit(self.cp_3p),
            "icu": self.icu,
        }


@dataclass
class FtpStatus:
    """Result of :func:`compute_ftp_status`."""

    as_of: dt.date
    current_ftp: float | None
    weight_kg: float | None
    windows: dict[str, WindowFit]
    estimate_source: str
    estimates: dict[dt.date, float]
    proposal: pdc.FtpProposal | None
    history: list[dict[str, Any]]

    @property
    def explanations(self) -> list[Explanation]:
        """Every Explanation produced (fits + proposal)."""
        out = [e for w in self.windows.values() for e in w.explanations]
        if self.proposal is not None and self.proposal.explanation is not None:
            out.append(self.proposal.explanation)
        return out

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict (API / reports)."""
        p = self.proposal
        return {
            "as_of": self.as_of.isoformat(),
            "current_ftp": self.current_ftp,
            "w_kg": round(self.current_ftp / self.weight_kg, 2)
            if self.current_ftp and self.weight_kg
            else None,
            "windows": {k: w.to_json() for k, w in self.windows.items()},
            "estimate_source": self.estimate_source,
            "estimates": [
                {"date": d.isoformat(), "ftp": round(v, 1)}
                for d, v in sorted(self.estimates.items())
            ],
            "proposal": None
            if p is None
            else {
                "proposed_ftp": p.proposed_ftp,
                "direction": p.direction,
                "days_sustained": p.days_sustained,
                "median_estimate": p.median_estimate,
                "change_pct": p.change_pct,
                "best_20min_w": p.best_20min_w,
                "unsupported": p.unsupported,
                "sources": p.sources,
            },
            "history": self.history,
            "explanations": [e.to_json_dict() for e in self.explanations],
        }


def _icu_model(icu: Mapping[str, float | None] | None) -> dict[str, Any] | None:
    if not icu:
        return None
    return {
        "criticalPower": icu.get("cp"),
        "wPrime": icu.get("w_prime"),
        "pMax": icu.get("p_max"),
        "ftp": icu.get("eftp"),
    }


def _curve_matrix(
    ds: Dataset, start: dt.date, end: dt.date
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """(day ordinals, rides × durations watts matrix with NaN for missing, durations)."""
    rides = [r for r in ds.rides(start, end) if r.measured_power and r.power_curve]
    durations = sorted({d for r in rides for d in r.power_curve})
    m = np.full((len(rides), len(durations)), np.nan)
    col = {d: j for j, d in enumerate(durations)}
    for i, r in enumerate(rides):
        for d, w in r.power_curve.items():
            m[i, col[d]] = w
    days = np.array([r.date.toordinal() for r in rides], dtype=np.int64)
    return days, m, durations


def _mmp(
    days: np.ndarray, m: np.ndarray, durations: list[int], lo: dt.date, hi: dt.date
) -> dict[int, float]:
    if m.size == 0:
        return {}
    mask = (days >= lo.toordinal()) & (days <= hi.toordinal())
    if not mask.any():
        return {}
    with np.errstate(all="ignore"):
        best = np.nanmax(np.where(mask[:, None], m, np.nan), axis=0)
    return {d: float(w) for d, w in zip(durations, best, strict=True) if np.isfinite(w) and w > 0}


def compute_ftp_status(
    ds: Dataset, as_of: dt.date, *, overrides: Mapping[str, float] | None = None
) -> FtpStatus:
    """See module docstring."""
    ov = dict(overrides or {})
    ftp = ov.get("ftp") or ds.ftp_on(as_of)
    start = as_of - dt.timedelta(days=max(WINDOWS.values()) + SERIES_DAYS)
    days, m, durations = _curve_matrix(ds, start, as_of)

    windows: dict[str, WindowFit] = {}
    for name, n in WINDOWS.items():
        mmp = _mmp(days, m, durations, as_of - dt.timedelta(days=n - 1), as_of)
        fit2, fit3 = pdc.fit_cp_2p(mmp), pdc.fit_cp_3p(mmp)
        icu = ds.icu_models.get(name)
        wf = WindowFit(name, mmp, fit2, fit3, dict(icu) if icu else None)
        if name == "42d":
            for f in (fit2, fit3):
                if f is not None:
                    wf.explanations.append(
                        pdc.explain_fit(f, pdc.compare_with_icu(f, _icu_model(icu)), as_of=as_of)
                    )
        windows[name] = wf

    series_start = as_of - dt.timedelta(days=SERIES_DAYS - 1)
    icu_points = {d: w.eftp for d, w in ds.wellness.items() if w.eftp is not None and d <= as_of}
    if icu_points:
        source = "icu_eftp"
        estimates = pdc.daily_series(icu_points, series_start, as_of)
    else:
        source = "cyp_cp_2p_42d"
        estimates = {}
        for i in range(SERIES_DAYS):
            day = series_start + dt.timedelta(days=i)
            fit = pdc.fit_cp_2p(
                _mmp(days, m, durations, day - dt.timedelta(days=ROLLING_WINDOW - 1), day)
            )
            if fit is not None:
                estimates[day] = fit.eftp

    proposal = None
    if ftp:
        proposal = pdc.ftp_proposal(
            float(ftp),
            estimates,
            as_of=as_of,
            sources=[source],
            best_20min_w=windows["42d"].mmp.get(1200),
        )
    history = [
        {"effective_from": s.effective_from.isoformat(), "ftp": s.ftp, "source": s.source}
        for s in ds.settings
        if s.ftp
    ]
    return FtpStatus(
        as_of,
        float(ftp) if ftp else None,
        ds.weight_kg,
        windows,
        source,
        estimates,
        proposal,
        history,
    )
