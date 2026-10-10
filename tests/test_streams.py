"""Parquet StreamStore roundtrip and schema validation."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from cyp.core.errors import NotFoundError, SchemaError
from cyp.store.streams import STREAM_SCHEMA, StreamStore, conform


def _frame(n: int = 10) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "t_s": list(range(n)),
            "watts": [200 + i for i in range(n)],
            "hr": [140 + i for i in range(n)],
            "moving": [True] * n,
            "alt_m": [100.0 + i * 0.5 for i in range(n)],
        }
    )


def test_roundtrip_casts_to_schema(tmp_path: Path) -> None:
    store = StreamStore(tmp_path / "streams")
    path = store.write(42, _frame())
    assert path == tmp_path / "streams" / "42.parquet"
    assert store.exists(42)
    back = store.read(42)
    assert back.shape == (10, 5)
    assert back.schema["t_s"] == STREAM_SCHEMA["t_s"]
    assert back.schema["watts"] == pl.Int16
    assert back.schema["alt_m"] == pl.Float32
    assert back.schema["moving"] == pl.Boolean
    assert back["watts"].to_list() == [200 + i for i in range(10)]
    assert store.list_ids() == [42]
    assert store.read(42, columns=["t_s", "hr"]).columns == ["t_s", "hr"]


def test_unknown_column_rejected() -> None:
    with pytest.raises(SchemaError, match="unknown"):
        conform(pl.DataFrame({"t_s": [0, 1], "power": [1, 2]}))


def test_missing_t_s_rejected() -> None:
    with pytest.raises(SchemaError, match="t_s"):
        conform(pl.DataFrame({"watts": [1, 2]}))


def test_overflow_cast_rejected() -> None:
    with pytest.raises(SchemaError, match="cast"):
        conform(pl.DataFrame({"t_s": [0], "watts": [70000]}))


def test_read_missing_raises(tmp_path: Path) -> None:
    store = StreamStore(tmp_path / "streams")
    with pytest.raises(NotFoundError):
        store.read(1)
    assert store.delete(1) is False
    assert store.list_ids() == []


def test_scan_all(tmp_path: Path) -> None:
    store = StreamStore(tmp_path / "streams")
    store.write(1, _frame(5))
    store.write(2, _frame(7))
    total = store.scan_all().select(pl.len()).collect().item()
    assert total == 12
