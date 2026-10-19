"""Parquet-per-activity stream store (docs/03 ``stream_files``).

One file per activity at ``{streams_dir}/{activity_id}.parquet`` with the fixed schema below.
A written frame may contain any subset of the columns (``t_s`` is mandatory); unknown columns
are rejected and dtypes are cast to the canonical schema so cross-ride ``scan_parquet`` works.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from cyp.core.activity import StreamName
from cyp.core.errors import NotFoundError, SchemaError

#: Canonical Parquet schema (docs/03-data-model.md, ``stream_files``).
STREAM_SCHEMA: dict[str, pl.DataType] = {
    StreamName.T_S: pl.Int32(),
    StreamName.DIST_M: pl.Float32(),
    StreamName.LAT: pl.Float64(),
    StreamName.LNG: pl.Float64(),
    StreamName.ALT_M: pl.Float32(),
    StreamName.SPEED_MPS: pl.Float32(),
    StreamName.HR: pl.Int16(),
    StreamName.CAD: pl.Int16(),
    StreamName.WATTS: pl.Int16(),
    StreamName.WATTS_EST: pl.Int16(),
    StreamName.TEMP_C: pl.Int8(),
    StreamName.MOVING: pl.Boolean(),
    StreamName.GRADE_PCT: pl.Float32(),
}

REQUIRED_COLUMNS: frozenset[str] = frozenset({StreamName.T_S})


def conform(df: pl.DataFrame) -> pl.DataFrame:
    """Validate column names and cast to the canonical dtypes.

    Raises:
        SchemaError: unknown columns, missing ``t_s``, or a cast that fails.
    """
    unknown = set(df.columns) - set(STREAM_SCHEMA)
    if unknown:
        raise SchemaError(f"unknown stream columns: {sorted(unknown)}")
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise SchemaError(f"missing required stream columns: {sorted(missing)}")
    try:
        casted = df.select(
            [
                pl.col(c).cast(STREAM_SCHEMA[c], strict=True)
                for c in STREAM_SCHEMA
                if c in df.columns
            ]
        )
    except pl.exceptions.PolarsError as exc:
        raise SchemaError(f"stream column cast failed: {exc}") from exc
    if casted[StreamName.T_S].null_count():
        raise SchemaError("t_s must not contain nulls")
    return casted


class StreamStore:
    """Read/write one Parquet file per activity under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, activity_id: int) -> Path:
        """Path of the Parquet file for ``activity_id`` (may not exist)."""
        return self.root / f"{activity_id}.parquet"

    def exists(self, activity_id: int) -> bool:
        """True if a stream file is present."""
        return self.path_for(activity_id).is_file()

    def write(self, activity_id: int, df: pl.DataFrame) -> Path:
        """Validate, cast and atomically write ``df``; returns the file path."""
        conformed = conform(df)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(activity_id)
        tmp = target.with_suffix(".parquet.tmp")
        conformed.write_parquet(tmp, compression="zstd")
        tmp.replace(target)
        return target

    def read(self, activity_id: int, columns: list[str] | None = None) -> pl.DataFrame:
        """Load the stream for ``activity_id``.

        Raises:
            NotFoundError: no file for that activity.
        """
        path = self.path_for(activity_id)
        if not path.is_file():
            raise NotFoundError(f"no stream file for activity {activity_id}: {path}")
        return pl.read_parquet(path, columns=columns)

    def delete(self, activity_id: int) -> bool:
        """Remove the stream file; returns whether anything was deleted."""
        path = self.path_for(activity_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    def scan_all(self) -> pl.LazyFrame:
        """Lazy scan over every stream file (longitudinal analysis)."""
        return pl.scan_parquet(str(self.root / "*.parquet"))

    def list_ids(self) -> list[int]:
        """Activity ids that have a stream file, ascending."""
        if not self.root.is_dir():
            return []
        ids = [int(p.stem) for p in self.root.glob("*.parquet") if p.stem.isdigit()]
        return sorted(ids)
