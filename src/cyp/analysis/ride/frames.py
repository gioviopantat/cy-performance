"""Ride stream frames: load the 1 Hz Parquet into numpy arrays plus shared smoothing helpers.

The store writes one Parquet per activity on a *complete* 1 Hz grid (``t_s`` = 0..elapsed);
seconds inside a recording pause are ``null`` in every data column. This module turns that
frame into a :class:`RideFrame` with two masks every metric builds on:

- ``recorded``: at least one data column is present at this second (the device was recording).
- ``moving``: the athlete was riding (Strava ``moving`` flag when present, else speed > 0.5 m/s,
  else ``recorded``). Stops at traffic lights are *not* moving; coasting downhill is.

kJ, average and max power are computed over ``recorded`` seconds; NP, TSS and time in zone use
``moving`` seconds (coasting zeros count, stationary seconds do not), matching intervals.icu.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np
import numpy.typing as npt
import polars as pl
from scipy.ndimage import median_filter, uniform_filter1d  # type: ignore[import-untyped]

from cyp.core.activity import StreamName
from cyp.core.errors import AnalysisError
from cyp.store.streams import StreamStore

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

#: Speed below which a sample counts as stopped when no ``moving`` stream exists (m/s).
MOVING_SPEED_MPS = 0.5
#: Coggan rolling window for NP and for the "physiological" power used in HR comparisons.
NP_WINDOW_S = 30
#: Altitude smoothing window (samples) — rolling median then rolling mean, as in strava-analyis.
ALT_SMOOTH_WINDOW = 21
#: Hysteresis (m) before a rise counts toward elevation gain (bike-computer convention).
ALT_GAIN_THRESHOLD_M = 5.0

_DATA_COLUMNS: tuple[str, ...] = tuple(c for c in StreamName if c != StreamName.T_S)


def _float_col(df: pl.DataFrame, name: str) -> FloatArray | None:
    if name not in df.columns:
        return None
    return np.asarray(df[name].cast(pl.Float64).to_numpy(), dtype=np.float64)


def fill_gaps(x: FloatArray) -> FloatArray:
    """Forward- then back-fill NaNs (linear index interpolation); all-NaN input is zeroed."""
    out = np.array(x, dtype=np.float64, copy=True)
    ok = np.isfinite(out)
    if not ok.any():
        return np.zeros_like(out)
    if ok.all():
        return out
    idx = np.arange(out.size)
    out[~ok] = np.interp(idx[~ok], idx[ok], out[ok])
    return out


def rolling_mean(x: FloatArray, window: int) -> FloatArray:
    """Trailing rolling mean with ``min_periods == window``; leading values are NaN.

    ``x`` must be NaN-free (fill it first); ``window`` is clamped to ``[1, len(x)]``.
    """
    n = x.size
    if n == 0:
        return np.empty(0, dtype=np.float64)
    w = max(1, min(int(window), n))
    csum = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    out = np.full(n, np.nan, dtype=np.float64)
    out[w - 1 :] = (csum[w:] - csum[:-w]) / w
    return out


def smooth_altitude(alt: FloatArray, window: int = ALT_SMOOTH_WINDOW) -> FloatArray:
    """Gap-fill then rolling-median + rolling-mean an altitude series (GPS/baro noise reject)."""
    if alt.size == 0:
        return np.empty(0, dtype=np.float64)
    filled = fill_gaps(alt)
    win = max(1, min(int(window), filled.size))
    med = np.asarray(median_filter(filled, size=win, mode="nearest"), dtype=np.float64)
    return np.asarray(uniform_filter1d(med, size=win, mode="nearest"), dtype=np.float64)


def elevation_gain(alt_smoothed: FloatArray, threshold_m: float = ALT_GAIN_THRESHOLD_M) -> float:
    """Total ascent with hysteresis: a rise counts once it clears the trough by ``threshold_m``."""
    if alt_smoothed.size < 2:
        return 0.0
    total = 0.0
    trough = float(alt_smoothed[0])
    committed = trough
    for v in alt_smoothed[1:]:
        value = float(v)
        if value < trough:
            trough = value
            committed = min(committed, value)
        elif value - trough >= threshold_m and value > committed:
            total += value - committed
            committed = value
            trough = value
    return total


def recorded_mask(df: pl.DataFrame) -> BoolArray:
    """True where any data column (anything but ``t_s``) is non-null."""
    cols = [c for c in _DATA_COLUMNS if c in df.columns]
    if not cols:
        return np.ones(df.height, dtype=bool)
    expr = pl.any_horizontal([pl.col(c).is_not_null() for c in cols])
    return np.asarray(df.select(expr.alias("r"))["r"].to_numpy(), dtype=bool)


def moving_mask(df: pl.DataFrame, recorded: BoolArray | None = None) -> BoolArray:
    """Moving seconds: Strava ``moving`` flag, else speed > 0.5 m/s, else ``recorded``."""
    rec = recorded_mask(df) if recorded is None else recorded
    if StreamName.MOVING in df.columns:
        flag = np.asarray(
            df[StreamName.MOVING].fill_null(False).cast(pl.Boolean).to_numpy(), dtype=bool
        )
        if flag.any():
            return flag & rec
    speed = _float_col(df, StreamName.SPEED_MPS)
    if speed is not None and np.isfinite(speed).any():
        return (np.nan_to_num(speed, nan=0.0) > MOVING_SPEED_MPS) & rec
    return rec


@dataclass
class RideFrame:
    """A ride's 1 Hz streams as float arrays (NaN = missing) with recorded / moving masks."""

    df: pl.DataFrame
    recorded: BoolArray
    moving: BoolArray

    @classmethod
    def from_df(cls, df: pl.DataFrame) -> RideFrame:
        """Build from a stream frame; sorts by ``t_s`` and requires a contiguous 1 Hz grid.

        Raises:
            AnalysisError: ``t_s`` missing or not a contiguous 1 Hz grid.
        """
        if StreamName.T_S not in df.columns:
            raise AnalysisError("stream frame has no t_s column")
        if df.height == 0:
            raise AnalysisError("stream frame is empty")
        df = df.sort(StreamName.T_S)
        t = np.asarray(df[StreamName.T_S].cast(pl.Int64).to_numpy(), dtype=np.int64)
        if t.size > 1 and not np.all(np.diff(t) == 1):
            raise AnalysisError("stream frame is not a contiguous 1 Hz grid")
        rec = recorded_mask(df)
        return cls(df=df, recorded=rec, moving=moving_mask(df, rec))

    # ---------------------------------------------------------------- columns
    def col(self, name: str) -> FloatArray | None:
        """Column as float64 with NaN for nulls, or ``None`` when absent."""
        return _float_col(self.df, name)

    def has(self, name: str) -> bool:
        """True when the column exists and has at least one non-null value."""
        return name in self.df.columns and self.df[name].null_count() < self.df.height

    @cached_property
    def t_s(self) -> npt.NDArray[np.int64]:
        """Seconds since start, 0..elapsed."""
        return np.asarray(self.df[StreamName.T_S].cast(pl.Int64).to_numpy(), dtype=np.int64)

    @cached_property
    def watts(self) -> FloatArray | None:
        """Measured power (W)."""
        return self.col(StreamName.WATTS)

    @cached_property
    def hr(self) -> FloatArray | None:
        """Heart rate (bpm)."""
        return self.col(StreamName.HR)

    @cached_property
    def speed(self) -> FloatArray | None:
        """Ground speed (m/s)."""
        return self.col(StreamName.SPEED_MPS)

    @cached_property
    def alt(self) -> FloatArray | None:
        """Altitude (m)."""
        return self.col(StreamName.ALT_M)

    @cached_property
    def dist(self) -> FloatArray | None:
        """Cumulative distance (m)."""
        return self.col(StreamName.DIST_M)

    @cached_property
    def grade(self) -> FloatArray | None:
        """Grade (%)."""
        return self.col(StreamName.GRADE_PCT)

    @cached_property
    def lat(self) -> FloatArray | None:
        """Latitude (deg)."""
        return self.col(StreamName.LAT)

    @cached_property
    def lng(self) -> FloatArray | None:
        """Longitude (deg)."""
        return self.col(StreamName.LNG)

    @cached_property
    def cad(self) -> FloatArray | None:
        """Cadence (rpm)."""
        return self.col(StreamName.CAD)

    # ---------------------------------------------------------------- durations
    @property
    def n(self) -> int:
        """Number of grid seconds (elapsed + 1)."""
        return self.df.height

    @property
    def elapsed_s(self) -> int:
        """Last ``t_s`` minus first."""
        return int(self.t_s[-1] - self.t_s[0]) if self.n else 0

    @property
    def recording_s(self) -> int:
        """Seconds with data (pauses excluded)."""
        return int(self.recorded.sum())

    @property
    def moving_s(self) -> int:
        """Seconds spent moving."""
        return int(self.moving.sum())

    # ---------------------------------------------------------------- helpers
    def smoothed_altitude(self) -> FloatArray | None:
        """Smoothed altitude (see :func:`smooth_altitude`) or ``None`` without altitude."""
        if self.alt is None or not np.isfinite(self.alt).any():
            return None
        return smooth_altitude(self.alt)

    def power_30s(self, watts: FloatArray | None = None) -> FloatArray | None:
        """30 s trailing rolling mean of power over the full grid (NaN -> 0 W first)."""
        source = self.watts if watts is None else watts
        if source is None:
            return None
        return rolling_mean(np.nan_to_num(source, nan=0.0), NP_WINDOW_S)


def load_frame(store: StreamStore, activity_id: int) -> RideFrame:
    """Read ``{activity_id}.parquet`` through the store and wrap it.

    Raises:
        NotFoundError: no stream file.
        AnalysisError: malformed frame.
    """
    return RideFrame.from_df(store.read(activity_id))
