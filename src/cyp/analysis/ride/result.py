"""Input / output containers for the per-ride analysis (pure data, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cyp.analysis.ride.durability import DEFAULT_THRESHOLDS, Thresholds
from cyp.core.athlete import ZoneModel
from cyp.core.explain import Explanation

TssSource = Literal["power", "hr", "estimated"]


@dataclass
class RideInputs:
    """Everything the pure analysis needs besides the stream frame."""

    activity_id: int
    has_power: bool
    has_hr: bool
    race: bool = False
    trainer: bool = False
    ftp: float | None = None
    ftp_source: str = "none"
    weight_kg: float | None = None
    lthr: float | None = None
    max_hr: float | None = None
    resting_hr: float | None = None
    cp: float | None = None
    w_prime: float | None = None
    power_zones: ZoneModel | None = None
    hr_zones: ZoneModel | None = None
    zones_source: str = "none"
    # intervals.icu references for the cross-check
    icu_training_load: float | None = None
    icu_np_w: float | None = None
    icu_intensity: float | None = None
    icu_decoupling: float | None = None
    icu_ftp: float | None = None
    thresholds: Thresholds = DEFAULT_THRESHOLDS


@dataclass
class RideMetrics:
    """Result of :func:`cyp.analysis.ride.pipeline.compute_ride_metrics`.

    Field names mirror ``activity_metrics`` columns; ``to_row`` builds the upsert dict.
    """

    activity_id: int
    algo_version: str
    tss_source: TssSource
    np_w: float | None = None
    if_: float | None = None
    tss: float | None = None
    vi: float | None = None
    avg_w: float | None = None
    max_w: float | None = None
    kj: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    ef: float | None = None
    decoupling_pct: float | None = None
    decoupling_reliable: bool = False
    hr_lag_s: float | None = None
    hr_lag_corr: float | None = None
    hr_drift_detail: dict[str, Any] | None = None
    power_curve: dict[str, float] = field(default_factory=dict)
    power_curve_wkg: dict[str, float] = field(default_factory=dict)
    time_in_zone_power: dict[str, float] | None = None
    time_in_zone_hr: dict[str, float] | None = None
    climbs: list[dict[str, Any]] = field(default_factory=list)
    efforts: list[dict[str, Any]] = field(default_factory=list)
    pacing: dict[str, Any] = field(default_factory=dict)
    wbal_min_j: float | None = None
    estimated_power_meta: dict[str, Any] | None = None
    comparison: dict[str, Any] = field(default_factory=dict)
    durability: dict[str, Any] | None = None
    classification: str = "mixed"
    status: str = "NORMAL"
    next_recommendation: str = "AS_PLANNED"
    moving_s: int = 0
    recording_s: int = 0
    elapsed_s: int = 0
    elev_gain_m: float | None = None
    explanation: Explanation | None = None

    def to_row(self) -> dict[str, Any]:
        """Column -> value dict for ``ActivityMetricsRepo.upsert``."""
        return {
            "algo_version": self.algo_version,
            "np_w": _r(self.np_w, 1),
            "if_": _r(self.if_, 4),
            "tss": _r(self.tss, 1),
            "vi": _r(self.vi, 3),
            "tss_source": self.tss_source,
            "ef": _r(self.ef, 4),
            "decoupling_pct": _r(self.decoupling_pct, 2),
            "hr_lag_s": self.hr_lag_s,
            "hr_drift_detail": self.hr_drift_detail,
            "power_curve": {
                "watts": {k: round(v, 1) for k, v in self.power_curve.items()},
                "w_kg": {k: round(v, 3) for k, v in self.power_curve_wkg.items()},
            },
            "time_in_zone_power": self.time_in_zone_power,
            "time_in_zone_hr": self.time_in_zone_hr,
            "climbs": self.climbs,
            "efforts": self.efforts,
            "pacing": self.pacing,
            "wbal_min_j": _r(self.wbal_min_j, 0),
            "estimated_power_meta": self.estimated_power_meta,
            "comparison": self.comparison,
            "durability": self.durability,
            "status": self.status,
            "next_recommendation": self.next_recommendation,
            "explanation": self.explanation.to_json_dict() if self.explanation else None,
        }


def _r(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)
