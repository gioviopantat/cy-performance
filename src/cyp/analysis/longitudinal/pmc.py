"""Banister PMC replay, load-safety metrics and forward simulation (docs/04 §3).

Pure functions over a ``{date: load}`` series; no I/O. Conventions (docs/glossary/banister_pmc.md,
ramp_rate_acwr_monotony.md):

- ``CTL_d = CTL_{d-1} + (L_d - CTL_{d-1}) * k_ctl`` and the same for ATL, where ``L_d`` is the
  day's total load (all sports) and ``k = 1 - exp(-1/τ)`` (``decay="exp"``, intervals.icu's
  form) or ``k = 1/τ`` (``decay="linear"``, the classic TrainingPeaks form). τ = 42 / 7.
  The value for day *d* already includes day *d*'s load (icu's convention).
- Seeded from icu's CTL/ATL on the seed day (the replay never invents history); the seed day's
  own value is copied, replay starts the day after.
- ``TSB_d = CTL_d - ATL_d`` (form *after* day d's training; icu's daily "form").
- Ramp rate = ``CTL_d - CTL_{d-7}``.
- ACWR (uncoupled 7:28) = mean load of the last 7 days / mean load of the 28 days before them.
- Foster monotony = mean / sd of the last 7 daily loads (sd 0 -> ``None``); strain = weekly
  sum * monotony.
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from cyp.core.explain import Explanation, MethodRef, Reason

Decay = Literal["exp", "linear"]
MODEL_ID = "banister_pmc"
PMC_VERSION = "pmc_v1"
CTL_TAU = 42.0
ATL_TAU = 7.0
#: CTL agreement target with intervals.icu (docs/04 §3).
ICU_TOLERANCE = 1.0


@dataclass(frozen=True)
class PMCParams:
    """Time constants and decay form."""

    ctl_tau: float = CTL_TAU
    atl_tau: float = ATL_TAU
    decay: Decay = "exp"

    def k(self, tau: float) -> float:
        """Per-day update coefficient for time constant ``tau``."""
        return 1.0 - math.exp(-1.0 / tau) if self.decay == "exp" else 1.0 / tau


DEFAULT_PARAMS = PMCParams()


@dataclass(frozen=True)
class PMCState:
    """CTL/ATL on one day."""

    date: dt.date
    ctl: float
    atl: float

    @property
    def tsb(self) -> float:
        """Form = CTL - ATL."""
        return self.ctl - self.atl


@dataclass
class PMCDay:
    """One replayed day plus load-safety metrics."""

    date: dt.date
    load: float
    ctl: float
    atl: float
    tsb: float
    ramp_rate: float | None = None
    acwr_7_28: float | None = None
    monotony_7: float | None = None
    strain_7: float | None = None


def days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Inclusive list of calendar days."""
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def step(state: PMCState, load: float, params: PMCParams = DEFAULT_PARAMS) -> PMCState:
    """Advance one day with ``load``."""
    k_c, k_a = params.k(params.ctl_tau), params.k(params.atl_tau)
    return PMCState(
        date=state.date + dt.timedelta(days=1),
        ctl=state.ctl + (load - state.ctl) * k_c,
        atl=state.atl + (load - state.atl) * k_a,
    )


def replay(
    seed: PMCState,
    loads: Mapping[dt.date, float],
    end: dt.date,
    params: PMCParams = DEFAULT_PARAMS,
    *,
    annotate: bool = True,
) -> list[PMCDay]:
    """Replay from ``seed`` (included as the first day) through ``end``.

    Days missing from ``loads`` count as 0 load. Load-safety metrics use ``loads`` before the
    seed too, so pass history when available.
    """
    out: list[PMCDay] = [
        PMCDay(
            date=seed.date,
            load=float(loads.get(seed.date, 0.0)),
            ctl=seed.ctl,
            atl=seed.atl,
            tsb=seed.tsb,
        )
    ]
    state = seed
    while state.date < end:
        nxt = state.date + dt.timedelta(days=1)
        state = step(state, float(loads.get(nxt, 0.0)), params)
        out.append(
            PMCDay(
                date=nxt,
                load=float(loads.get(nxt, 0.0)),
                ctl=state.ctl,
                atl=state.atl,
                tsb=state.tsb,
            )
        )
    if annotate:
        _annotate_safety(out, loads)
    return out


def simulate(
    seed: PMCState, future_loads: Iterable[float], params: PMCParams = DEFAULT_PARAMS
) -> list[PMCState]:
    """Project forward from ``seed`` with one load per future day (seed day excluded).

    The planner's what-if tool: "if I do these TSS on the next N days, where do CTL/ATL/TSB end?"
    """
    out: list[PMCState] = []
    state = seed
    for load in future_loads:
        state = step(state, float(load), params)
        out.append(state)
    return out


def load_for_ctl_target(
    seed: PMCState, target_ctl: float, n_days: int, params: PMCParams = DEFAULT_PARAMS
) -> float:
    """Constant daily load that brings CTL from ``seed.ctl`` to ``target_ctl`` in ``n_days``.

    Closed form of the recursion: ``CTL_n = L + (CTL_0 - L) * (1-k)^n``.
    """
    if n_days <= 0:
        raise ValueError("n_days must be positive")
    r = (1.0 - params.k(params.ctl_tau)) ** n_days
    return max((target_ctl - seed.ctl * r) / (1.0 - r), 0.0)


def _window(loads: Mapping[dt.date, float], end: dt.date, n: int) -> list[float]:
    return [float(loads.get(end - dt.timedelta(days=i), 0.0)) for i in range(n)]


def acwr(loads: Mapping[dt.date, float], day: dt.date) -> float | None:
    """Uncoupled 7:28 acute:chronic workload ratio on ``day`` (``None`` if chronic is 0)."""
    acute = sum(_window(loads, day, 7)) / 7.0
    chronic = sum(_window(loads, day - dt.timedelta(days=7), 28)) / 28.0
    if chronic <= 0:
        return None
    return acute / chronic


def monotony_strain(
    loads: Mapping[dt.date, float], day: dt.date
) -> tuple[float | None, float | None]:
    """Foster monotony and strain over the 7 days ending ``day``."""
    w = _window(loads, day, 7)
    total = sum(w)
    if total <= 0:
        return None, None
    sd = statistics.pstdev(w)
    if sd <= 1e-9:
        return None, None
    mono = (total / 7.0) / sd
    return mono, total * mono


def _annotate_safety(series: list[PMCDay], loads: Mapping[dt.date, float]) -> None:
    """Vectorised ramp / ACWR / monotony / strain (same definitions as :func:`acwr` etc.)."""
    if not series:
        return
    first = series[0].date - dt.timedelta(days=34)
    n = (series[-1].date - first).days + 1
    arr = np.zeros(n)
    for d, v in loads.items():
        i = (d - first).days
        if 0 <= i < n:
            arr[i] = v
    cs = np.concatenate([[0.0], np.cumsum(arr)])
    cs2 = np.concatenate([[0.0], np.cumsum(arr * arr)])
    idx = np.array([(d.date - first).days for d in series])
    week = cs[idx + 1] - cs[idx - 6]
    chronic = (cs[idx - 6] - cs[idx - 34]) / 28.0
    mean = week / 7.0
    var = np.maximum((cs2[idx + 1] - cs2[idx - 6]) / 7.0 - mean * mean, 0.0)
    sd = np.sqrt(var)
    ctl = np.array([d.ctl for d in series])
    for k, day in enumerate(series):
        day.ramp_rate = float(ctl[k] - ctl[k - 7]) if k >= 7 else None
        day.acwr_7_28 = float(mean[k] / chronic[k]) if chronic[k] > 0 else None
        if week[k] > 0 and sd[k] > 1e-9:
            day.monotony_7 = float(mean[k] / sd[k])
            day.strain_7 = float(week[k] * day.monotony_7)
        else:
            day.monotony_7 = day.strain_7 = None


# ------------------------------------------------------------------------- icu comparison


@dataclass
class IcuAgreement:
    """How well a replay tracks icu's own CTL/ATL."""

    params: PMCParams
    n_days: int
    max_abs_ctl_err: float
    mean_abs_ctl_err: float
    max_abs_atl_err: float
    worst_day: dt.date | None
    within_tolerance: bool
    diffs: list[tuple[dt.date, float, float]] = field(default_factory=list)


def compare_with_icu(
    series: Iterable[PMCDay],
    icu: Mapping[dt.date, tuple[float | None, float | None]],
    params: PMCParams,
    tolerance: float = ICU_TOLERANCE,
) -> IcuAgreement:
    """CTL/ATL error of ``series`` against icu's ``{date: (ctl, atl)}``."""
    ctl_err: list[float] = []
    atl_err: list[float] = []
    worst: tuple[float, dt.date] | None = None
    diffs: list[tuple[dt.date, float, float]] = []
    for d in series:
        ref = icu.get(d.date)
        if ref is None or ref[0] is None:
            continue
        e = d.ctl - ref[0]
        ctl_err.append(abs(e))
        ea = d.atl - ref[1] if ref[1] is not None else 0.0
        atl_err.append(abs(ea))
        diffs.append((d.date, e, ea))
        if worst is None or abs(e) > worst[0]:
            worst = (abs(e), d.date)
    n = len(ctl_err)
    max_ctl = max(ctl_err) if ctl_err else 0.0
    return IcuAgreement(
        params=params,
        n_days=n,
        max_abs_ctl_err=max_ctl,
        mean_abs_ctl_err=sum(ctl_err) / n if n else 0.0,
        max_abs_atl_err=max(atl_err) if atl_err else 0.0,
        worst_day=worst[1] if worst else None,
        within_tolerance=n > 0 and max_ctl <= tolerance,
        diffs=diffs,
    )


def best_params(
    seed: PMCState,
    loads: Mapping[dt.date, float],
    end: dt.date,
    icu: Mapping[dt.date, tuple[float | None, float | None]],
) -> tuple[list[PMCDay], IcuAgreement]:
    """Replay with both decay forms and keep the one that tracks icu better."""
    best: tuple[list[PMCDay], IcuAgreement] | None = None
    for decay in ("exp", "linear"):
        params = PMCParams(decay=decay)  # type: ignore[arg-type]
        series = replay(seed, loads, end, params, annotate=False)
        agreement = compare_with_icu(series, icu, params)
        if best is None or agreement.mean_abs_ctl_err < best[1].mean_abs_ctl_err:
            best = (series, agreement)
    assert best is not None
    _annotate_safety(best[0], loads)
    return best


def explain_pmc(day: PMCDay, agreement: IcuAgreement | None) -> Explanation:
    """Explanation for the replayed fitness state on ``day``."""
    because = [
        Reason(
            text_zh=f"體能（CTL）{day.ctl:.1f}、疲勞（ATL）{day.atl:.1f}，狀態（TSB）{day.tsb:+.1f}",
            evidence={"ctl": round(day.ctl, 2), "atl": round(day.atl, 2), "tsb": round(day.tsb, 2)},
        )
    ]
    if day.ramp_rate is not None:
        because.append(
            Reason(
                text_zh=f"過去 7 天 CTL 變化 {day.ramp_rate:+.1f}（ramp rate）",
                evidence={"ramp_rate": round(day.ramp_rate, 2)},
            )
        )
    if day.acwr_7_28 is not None:
        because.append(
            Reason(
                text_zh=f"急慢性負荷比 ACWR {day.acwr_7_28:.2f}（0.8–1.3 為安全區）",
                evidence={"acwr_7_28": round(day.acwr_7_28, 3)},
            )
        )
    confidence: Literal["high", "medium", "low"] = "medium"
    if agreement is not None:
        ok = agreement.within_tolerance
        confidence = "high" if ok else "medium"
        because.append(
            Reason(
                text_zh=(
                    f"與 intervals.icu 對照 {agreement.n_days} 天，CTL 最大誤差 "
                    f"{agreement.max_abs_ctl_err:.2f}" + ("（在 ±1 內）" if ok else "（超出 ±1）")
                ),
                evidence={
                    "n_days": agreement.n_days,
                    "max_abs_ctl_err": round(agreement.max_abs_ctl_err, 3),
                    "decay": agreement.params.decay,
                },
            )
        )
    return Explanation(
        key=f"pmc.{day.date.isoformat()}",
        headline_zh=f"{day.date.isoformat()} 體能 {day.ctl:.0f}、狀態 {day.tsb:+.0f}",
        because=because,
        method=MethodRef(
            model_id=MODEL_ID,
            version=PMC_VERSION,
            inputs={
                "ctl_tau": CTL_TAU,
                "atl_tau": ATL_TAU,
                "decay": agreement.params.decay if agreement else "exp",
            },
            doc="docs/glossary/banister_pmc.md",
        ),
        confidence=confidence,
        glossary_terms=["banister_pmc", "ramp_rate_acwr_monotony"],
    )
