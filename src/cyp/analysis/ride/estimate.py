"""Estimators for rides without a power meter (port of strava-analyis ``estimate``).

Physics power (road-load equation, per 1 Hz sample)::

    theta  = atan(grade / 100)
    P = (crr*m*g*cos(theta) + m*g*sin(theta) + 0.5*rho*cda*v^2 + m*a) * v / drivetrain

clamped at 0 W, with ``m`` = rider + 8 kg bike, ``cda`` 0.32 m², ``crr`` 0.005, ``rho`` 1.225,
drivetrain 0.975, no wind, acceleration smoothed over 5 s.

HR training stress (``hr_tss``): Banister TRIMP-exp over moving seconds using heart-rate
reserve, scaled so one hour exactly at LTHR = 100 (same meaning as TSS).

Every estimator returns a Traditional-Chinese method string so reports can label the number
``(估算)`` and never confuse it with measured data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import uniform_filter1d  # type: ignore[import-untyped]

from cyp.analysis.ride.frames import FloatArray, RideFrame, fill_gaps

G = 9.80665
DEFAULT_BIKE_MASS_KG = 8.0
DEFAULT_CDA = 0.32
DEFAULT_CRR = 0.005
DEFAULT_RHO = 1.225
DEFAULT_DRIVETRAIN = 0.975
ACCEL_SMOOTH_WINDOW = 5
TRIMP_B = 1.92
TRIMP_K = 0.64
RESTING_HR_DEFAULT = 60


def estimate_power_series(
    frame: RideFrame,
    weight_kg: float | None,
    *,
    bike_mass_kg: float = DEFAULT_BIKE_MASS_KG,
    cda: float = DEFAULT_CDA,
    crr: float = DEFAULT_CRR,
    rho: float = DEFAULT_RHO,
    drivetrain: float = DEFAULT_DRIVETRAIN,
) -> FloatArray | None:
    """Physics power estimate over the full grid (NaN where the device was paused).

    Returns ``None`` without weight, speed or grade.
    """
    if not weight_kg or weight_kg <= 0:
        return None
    if frame.speed is None or frame.grade is None:
        return None
    if not np.isfinite(frame.speed).any() or not np.isfinite(frame.grade).any():
        return None
    v = fill_gaps(frame.speed)
    grade = fill_gaps(frame.grade)
    theta = np.arctan(grade / 100.0)
    m_total = weight_kg + bike_mass_kg
    a = np.gradient(v) if v.size > 1 else np.zeros_like(v)
    if ACCEL_SMOOTH_WINDOW > 1 and a.size >= ACCEL_SMOOTH_WINDOW:
        a = np.asarray(uniform_filter1d(a, size=ACCEL_SMOOTH_WINDOW, mode="nearest"))
    f_roll = crr * m_total * G * np.cos(theta)
    f_grav = m_total * G * np.sin(theta)
    f_air = 0.5 * rho * cda * v * v
    f_accel = m_total * a
    power = (f_roll + f_grav + f_air + f_accel) * v / drivetrain
    power = np.where(np.isfinite(power), power, 0.0)
    power = np.clip(power, 0.0, None)
    power = np.where(frame.recorded, power, np.nan)
    return np.asarray(power, dtype=np.float64)


def power_method_string(
    weight_kg: float,
    *,
    bike_mass_kg: float = DEFAULT_BIKE_MASS_KG,
    cda: float = DEFAULT_CDA,
    crr: float = DEFAULT_CRR,
    rho: float = DEFAULT_RHO,
    drivetrain: float = DEFAULT_DRIVETRAIN,
) -> str:
    """zh-TW description of the physics model and its assumptions."""
    return (
        "依物理騎乘功率模型（道路負載方程）估算："
        f"以騎士體重 {weight_kg:g} kg+車重 {bike_mass_kg:g} kg 為總質量，"
        f"逐秒合計滾動阻力（Crr={crr:g}）、重力分量（依坡度）、"
        f"空氣阻力（CdA={cda:g}、空氣密度 rho={rho:g} kg/m³）與加速慣性，"
        f"乘以速度後除以傳動效率（{drivetrain:g}）；下坡/滑行的負功率歸零。"
        "假設無風、加速度經 5 秒平滑。無功率計，數值僅為估算。"
    )


def hr_tss(
    frame: RideFrame,
    *,
    lthr: float | None,
    max_hr: float | None,
    resting_hr: float | None,
) -> tuple[float | None, str]:
    """Heart-rate TSS: Banister TRIMP-exp normalised to 100 per hour at LTHR (module docstring)."""
    if frame.hr is None:
        return None, "無心率資料，無法估算心率訓練壓力"
    if not lthr or not max_hr:
        return None, "缺少 LTHR/最大心率，無法估算心率訓練壓力"
    rest = float(resting_hr) if resting_hr else float(RESTING_HR_DEFAULT)
    denom = float(max_hr) - rest
    if denom <= 0:
        return None, "最大心率不大於靜止心率，無法估算心率訓練壓力"
    hr = frame.hr[frame.moving]
    hr = hr[np.isfinite(hr) & (hr > 0)]
    if hr.size == 0:
        return None, "無有效移動心率樣本，無法估算心率訓練壓力"
    hrr = np.clip((hr - rest) / denom, 0.0, 1.0)
    trimp = float(np.sum((1.0 / 60.0) * hrr * TRIMP_K * np.exp(TRIMP_B * hrr)))
    hrr_lthr = max(0.0, min(1.0, (float(lthr) - rest) / denom))
    trimp_lthr_1h = 60.0 * hrr_lthr * TRIMP_K * math.exp(TRIMP_B * hrr_lthr)
    if trimp_lthr_1h <= 0:
        return None, "LTHR 校正基準為零，無法估算心率訓練壓力"
    method = (
        f"以 Banister TRIMP（指數型）對移動時間積分，心率以心率儲備標準化"
        f"（LTHR {lthr:g}、最大 {max_hr:g}、靜止 {rest:g} bpm），"
        "校正為「於 LTHR 維持 1 小時 = 100」（估算）"
    )
    return trimp / trimp_lthr_1h * 100.0, method


@dataclass
class EstimateResult:
    """Outcome of the no-power path."""

    power_est: FloatArray | None = None
    hr_tss: float | None = None
    methods: dict[str, str] = field(default_factory=dict)

    def to_meta(self) -> dict[str, object]:
        """JSON for ``activity_metrics.estimated_power_meta``."""
        return {
            "power_estimated": self.power_est is not None,
            "hr_tss": round(self.hr_tss, 1) if self.hr_tss is not None else None,
            "methods": dict(self.methods),
        }


def estimate_for_ride(
    frame: RideFrame,
    *,
    weight_kg: float | None,
    lthr: float | None,
    max_hr: float | None,
    resting_hr: float | None,
) -> EstimateResult:
    """Run both estimators for a ride without measured power."""
    out = EstimateResult()
    out.power_est = estimate_power_series(frame, weight_kg)
    if out.power_est is not None and weight_kg:
        out.methods["power"] = power_method_string(weight_kg)
    value, method = hr_tss(frame, lthr=lthr, max_hr=max_hr, resting_hr=resting_hr)
    out.hr_tss = value
    out.methods["hr_tss"] = method
    return out
