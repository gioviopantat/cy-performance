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
- Data quality (docs/04 §7): rides flagged ``power_unreliable`` never enter the MMP / fits /
  estimates. When such rides lie within :data:`ICU_EFTP_LOOKBACK_DAYS` of the series, icu's
  eFTP (computed by icu from the same files) is not used; our clean rolling CP series is.
- :func:`max_effort_evidence`: were there near-maximal attempts in the last 42 days? Without
  them a *down* proposal is withheld and best-effort limiters report ``insufficient_data``.
- ``overrides``: what-if inputs (e.g. ``{"ftp": 270}`` to see the proposal against another
  setting) without touching stored data.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from cyp.analysis.longitudinal import limiters as lim_mod
from cyp.analysis.longitudinal import pdc
from cyp.core.explain import Explanation
from cyp.dataset import ActivityRow, Dataset

WINDOWS: dict[str, int] = {"42d": 42, "90d": 90}
#: intervals.icu serves one ``mmp-model`` (its own, longer window). Comparing it with our 42-day
#: fit reported a bogus -30 % on real data, so it is only paired with the 90-day window.
ICU_MODEL_WINDOW = "90d"
SERIES_DAYS = 56
ROLLING_WINDOW = 42
#: Assumed lookback of icu's eFTP model; unreliable rides inside it taint icu's series.
ICU_EFTP_LOOKBACK_DAYS = 90
_TEST_NAME = re.compile(r"(?i)\bftp\b|ramp test|\btest\b|測驗|測試|20 ?min")


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
    max_efforts: lim_mod.MaxEffortEvidence = field(default_factory=lim_mod.MaxEffortEvidence)
    power_unreliable_excluded: int = 0

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
            "max_efforts": self.max_efforts.to_json(),
            "power_unreliable_excluded": self.power_unreliable_excluded,
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
                "insufficient_evidence": p.insufficient_evidence,
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
    rides = [r for r in ds.rides(start, end) if r.power_ok and r.power_curve]
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


def _is_test(a: ActivityRow) -> bool:
    return a.classification == "race" or bool(a.name and _TEST_NAME.search(a.name))


def max_effort_evidence(
    ds: Dataset, as_of: dt.date, *, window_days: int = lim_mod.EVIDENCE_WINDOW_DAYS
) -> lim_mod.MaxEffortEvidence:
    """Near-maximal attempts in ``(as_of - window_days, as_of]`` (limiters.py thresholds).

    Sources: tests (ride name / ``race``), the 20-min power-curve point (``power_ok`` rides),
    detected climbs (power only when ``power_ok``; heart rate always — it is valid even when
    the power meter is not).
    """
    ev = lim_mod.MaxEffortEvidence(window_days=window_days)
    lo = as_of - dt.timedelta(days=window_days - 1)
    for a in ds.rides(lo, as_of):
        ftp = ds.ftp_on(a.date)
        st = ds.settings_on(a.date)
        lthr = st.lthr if st else None
        base = {"activity_id": a.id, "date": a.date.isoformat()}
        if _is_test(a):
            item = base | {"kind": "test", "label_zh": f"測驗／比賽「{a.name or ''}」"}
            ev.sustained.append(item)
            ev.short.append(item)
            continue
        if a.power_ok and ftp:
            p20 = a.power_curve.get(1200)
            if p20 and p20 >= lim_mod.SUSTAINED_FTP_FRAC * ftp:
                ev.sustained.append(
                    base
                    | {
                        "kind": "power_20min",
                        "watts": round(p20),
                        "pct_ftp": round(p20 / ftp * 100),
                        "label_zh": f"20 分鐘 {p20:.0f} W（{p20 / ftp * 100:.0f} % FTP）",
                    }
                )
            p5 = a.power_curve.get(300)
            if p5 and p5 >= lim_mod.SHORT_FTP_FRAC * ftp:
                ev.short.append(
                    base
                    | {
                        "kind": "power_5min",
                        "watts": round(p5),
                        "pct_ftp": round(p5 / ftp * 100),
                        "label_zh": f"5 分鐘 {p5:.0f} W（{p5 / ftp * 100:.0f} % FTP）",
                    }
                )
        for c in a.climbs:
            secs = c.get("moving_s") or c.get("duration_s")
            if not isinstance(secs, int | float) or secs < lim_mod.SHORT_MIN_S:
                continue
            hr, w = c.get("avg_hr"), c.get("avg_w")
            pct_lthr = hr / lthr if isinstance(hr, int | float) and lthr else None
            pct_ftp = w / ftp if a.power_ok and isinstance(w, int | float) and ftp else None
            item = base | {
                "kind": "climb",
                "minutes": round(secs / 60, 1),
                "avg_hr": hr,
                "pct_lthr": round(pct_lthr * 100) if pct_lthr else None,
                "pct_ftp": round(pct_ftp * 100) if pct_ftp else None,
                "label_zh": f"爬坡 {secs / 60:.0f} 分鐘"
                + (f"、{pct_lthr * 100:.0f} % LTHR" if pct_lthr else "")
                + (f"、{pct_ftp * 100:.0f} % FTP" if pct_ftp else ""),
            }
            if secs >= lim_mod.SUSTAINED_MIN_S and (
                (pct_ftp or 0) >= lim_mod.SUSTAINED_FTP_FRAC
                or (pct_lthr or 0) >= lim_mod.SUSTAINED_LTHR_FRAC
            ):
                ev.sustained.append(item)
            if (pct_ftp or 0) >= lim_mod.SHORT_FTP_FRAC or (
                pct_lthr or 0
            ) >= lim_mod.SHORT_LTHR_FRAC:
                ev.short.append(item)
    return ev


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
        icu = ds.icu_models.get(name) if name == ICU_MODEL_WINDOW else None
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
    taint_lo = series_start - dt.timedelta(days=ICU_EFTP_LOOKBACK_DAYS)
    tainted = [a for a in ds.power_unreliable_rides if taint_lo <= a.date <= as_of]
    notes: list[str] = []
    if icu_points and tainted:
        notes.append(
            f"icu eFTP 由 {len(tainted)} 趟功率不可信的騎乘算出（最晚 "
            f"{max(a.date for a in tainted).isoformat()}），改用排除它們後的 42 天 CP 估計"
        )
        icu_points = {}
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

    max_efforts = max_effort_evidence(ds, as_of)
    proposal = None
    if ftp:
        proposal = pdc.ftp_proposal(
            float(ftp),
            estimates,
            as_of=as_of,
            sources=[source],
            best_20min_w=windows["42d"].mmp.get(1200),
            recent_max_effort=bool(max_efforts.sustained),
            evidence_window_days=max_efforts.window_days,
            notes_zh=notes,
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
        max_efforts,
        len(ds.power_unreliable_rides),
    )
