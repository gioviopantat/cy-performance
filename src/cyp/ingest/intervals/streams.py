"""Map intervals.icu stream payloads onto the Parquet stream schema at 1 Hz (docs/03).

icu returns ``[{type, name, data, ...}]`` where ``data`` is a list aligned with the ``time``
stream (seconds since start, may contain gaps for pauses and may be recorded at > 1 s). We:

1. rename streams to our columns (``heartrate`` -> ``hr``, ``latlng`` -> ``lat``/``lng``, …);
   icu's ``latlng`` carries latitude in ``data`` and longitude in ``data2``;
2. drop duplicate timestamps (keep last) and sort by ``t_s``;
3. upsample onto a complete 1 Hz index ``[t_min, t_max]``, linearly interpolating continuous
   columns across gaps of at most ``max_gap_s`` seconds; longer gaps stay null with
   ``moving=False`` so pauses are visible rather than invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from cyp.core.activity import StreamName
from cyp.core.errors import SchemaError

#: icu stream name -> our column (``latlng`` handled separately).
ICU_STREAM_TO_COLUMN: dict[str, str] = {
    "time": StreamName.T_S,
    "distance": StreamName.DIST_M,
    "altitude": StreamName.ALT_M,
    "velocity_smooth": StreamName.SPEED_MPS,
    "heartrate": StreamName.HR,
    "cadence": StreamName.CAD,
    "watts": StreamName.WATTS,
    "fixed_watts": StreamName.WATTS,
    "temp": StreamName.TEMP_C,
    "moving": StreamName.MOVING,
    "grade_smooth": StreamName.GRADE_PCT,
}

#: Columns that are interpolated across short gaps; everything else is forward-filled.
INTERPOLATED: tuple[str, ...] = (
    StreamName.DIST_M,
    StreamName.LAT,
    StreamName.LNG,
    StreamName.ALT_M,
    StreamName.SPEED_MPS,
    StreamName.HR,
    StreamName.CAD,
    StreamName.WATTS,
    StreamName.TEMP_C,
    StreamName.GRADE_PCT,
)

ROUNDED_INT: tuple[str, ...] = (StreamName.HR, StreamName.CAD, StreamName.WATTS, StreamName.TEMP_C)


@dataclass(frozen=True)
class ResampleInfo:
    """What the resampler did; persisted on ``stream_files``."""

    n_original: int
    n_samples: int
    original_resolution_s: float | None
    resampled: bool

    @property
    def resolution_label(self) -> str:
        """``"1s"`` when the source was already 1 Hz and gap-free, else the median step."""
        if not self.resampled:
            return "1s"
        if self.original_resolution_s is None:
            return "irregular"
        return f"{self.original_resolution_s:g}s"


def _split_latlng(data: list[Any], data2: Any) -> tuple[list[Any], list[Any]]:
    """Split icu's ``latlng`` stream into ``(lat, lng)``.

    icu returns latitude in ``data`` and longitude in ``data2`` (two parallel float lists,
    ``valueTypeIsArray: false``). A Strava-style list of ``[lat, lng]`` pairs in ``data`` is
    also accepted.
    """
    if isinstance(data2, list):
        return list(data), list(data2)
    lat = [p[0] if isinstance(p, list | tuple) and len(p) == 2 else None for p in data]
    lng = [p[1] if isinstance(p, list | tuple) and len(p) == 2 else None for p in data]
    return lat, lng


def streams_to_frame(payload: list[dict[str, Any]]) -> pl.DataFrame:
    """Build a raw (not yet resampled) frame from an icu streams payload.

    Raises:
        SchemaError: no ``time`` stream, or a stream whose length differs from ``time``.
    """
    columns: dict[str, list[Any]] = {}
    for stream in payload:
        if stream.get("allNull"):
            continue
        name = str(stream.get("type") or stream.get("name") or "")
        data = stream.get("data")
        if not isinstance(data, list):
            continue
        if name == "latlng":
            lat, lng = _split_latlng(data, stream.get("data2"))
            columns[StreamName.LAT] = lat
            columns[StreamName.LNG] = lng
            continue
        col = ICU_STREAM_TO_COLUMN.get(name)
        if col is None:
            continue
        if col == StreamName.WATTS and col in columns and name != "watts":
            continue  # prefer the stream literally named "watts" (icu's fixed_watts alias)
        columns[col] = data
    if StreamName.T_S not in columns:
        raise SchemaError("icu streams payload has no 'time' stream")
    n = len(columns[StreamName.T_S])
    for col, values in columns.items():
        if len(values) != n:
            raise SchemaError(f"icu stream {col!r} has {len(values)} samples, time has {n}")
    frame = pl.DataFrame(columns, strict=False)
    if StreamName.MOVING in frame.columns:
        frame = frame.with_columns(pl.col(StreamName.MOVING).cast(pl.Boolean, strict=False))
    return frame


def resample_1hz(raw: pl.DataFrame, *, max_gap_s: int = 10) -> tuple[pl.DataFrame, ResampleInfo]:
    """Upsample ``raw`` (with ``t_s``) onto a complete 1 Hz grid; see module docstring."""
    if raw.height == 0:
        raise SchemaError("cannot resample an empty stream frame")
    df = (
        raw.with_columns(pl.col(StreamName.T_S).cast(pl.Float64).round(0).cast(pl.Int64))
        .drop_nulls(StreamName.T_S)
        .unique(subset=[StreamName.T_S], keep="last")
        .sort(StreamName.T_S)
    )
    n_original = df.height
    t_min = int(df[StreamName.T_S].min())  # type: ignore[arg-type]
    t_max = int(df[StreamName.T_S].max())  # type: ignore[arg-type]
    steps = df[StreamName.T_S].diff().drop_nulls()
    median_step = float(steps.median()) if steps.len() else None  # type: ignore[arg-type]
    already_1hz = n_original == t_max - t_min + 1

    if already_1hz:
        out = df
    else:
        grid = pl.DataFrame({StreamName.T_S: pl.int_range(t_min, t_max + 1, eager=True)})
        marked = df.with_columns(pl.lit(True).alias("__present"))
        out = grid.join(marked, on=StreamName.T_S, how="left")
        out = out.with_columns(pl.col("__present").fill_null(False))
        # Distance to the previous / next real sample bounds the gap each row sits in.
        prev_t = (
            pl.when(pl.col("__present")).then(pl.col(StreamName.T_S)).otherwise(None).forward_fill()
        )
        next_t = (
            pl.when(pl.col("__present"))
            .then(pl.col(StreamName.T_S))
            .otherwise(None)
            .backward_fill()
        )
        out = out.with_columns((next_t - prev_t).alias("__gap"))
        fill_ok = pl.col("__present") | (pl.col("__gap") <= max_gap_s)
        exprs: list[pl.Expr] = []
        for col in out.columns:
            if col in (StreamName.T_S, "__present", "__gap"):
                continue
            if col in INTERPOLATED:
                filled = pl.col(col).interpolate()
            else:
                filled = pl.col(col).forward_fill()
            expr = pl.when(fill_ok).then(filled).otherwise(None)
            if col == StreamName.MOVING:
                expr = expr.fill_null(False)
            exprs.append(expr.alias(col))
        out = out.with_columns(exprs).drop("__present", "__gap")

    round_exprs = [
        pl.col(c).round(0) for c in ROUNDED_INT if c in out.columns and out[c].dtype.is_float()
    ]
    if round_exprs:
        out = out.with_columns(round_exprs)
    out = out.with_columns((pl.col(StreamName.T_S) - t_min).alias(StreamName.T_S))
    info = ResampleInfo(
        n_original=n_original,
        n_samples=out.height,
        original_resolution_s=median_step,
        resampled=not already_1hz,
    )
    return out, info


def icu_streams_to_parquet_frame(
    payload: list[dict[str, Any]], *, max_gap_s: int = 10
) -> tuple[pl.DataFrame, ResampleInfo]:
    """``streams_to_frame`` + ``resample_1hz`` in one call."""
    return resample_1hz(streams_to_frame(payload), max_gap_s=max_gap_s)
