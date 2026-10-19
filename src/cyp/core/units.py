"""Unit conversions. Internal canonical units: metres, seconds, watts, kg, °C."""

from __future__ import annotations

KM_PER_M = 0.001
MI_PER_M = 1 / 1609.344
FT_PER_M = 1 / 0.3048
KMH_PER_MPS = 3.6


def m_to_km(m: float) -> float:
    """Metres to kilometres."""
    return m * KM_PER_M


def m_to_mi(m: float) -> float:
    """Metres to statute miles."""
    return m * MI_PER_M


def m_to_ft(m: float) -> float:
    """Metres to feet."""
    return m * FT_PER_M


def mps_to_kmh(mps: float) -> float:
    """Metres per second to km/h."""
    return mps * KMH_PER_MPS


def kmh_to_mps(kmh: float) -> float:
    """km/h to metres per second."""
    return kmh / KMH_PER_MPS


def watts_per_kg(watts: float, weight_kg: float) -> float:
    """W/kg; raises if weight is not positive."""
    if weight_kg <= 0:
        raise ValueError("weight_kg must be positive")
    return watts / weight_kg


def kj_from_watts(avg_watts: float, duration_s: float) -> float:
    """Mechanical work in kJ from average power and duration."""
    return avg_watts * duration_s / 1000.0


def pct_of_ftp(watts: float, ftp_w: float) -> float:
    """Power as a percentage of FTP."""
    if ftp_w <= 0:
        raise ValueError("ftp_w must be positive")
    return 100.0 * watts / ftp_w


def fmt_duration(seconds: float) -> str:
    """``h:mm:ss`` (or ``m:ss`` under an hour) for display."""
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
