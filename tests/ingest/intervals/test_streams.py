"""icu stream payload -> Parquet frame mapping and 1 Hz resampling."""

from __future__ import annotations

import polars as pl
import pytest

from cyp.core.errors import SchemaError
from cyp.ingest.intervals.streams import (
    icu_streams_to_parquet_frame,
    resample_1hz,
    streams_to_frame,
)
from cyp.store.streams import conform
from tests.ingest.intervals.conftest import load_fixture


def test_fixture_maps_to_schema_columns_and_resamples_to_1hz() -> None:
    payload = load_fixture("streams_i3001.json")
    raw = streams_to_frame(payload)
    assert raw.height == 600
    assert set(raw.columns) == {
        "t_s", "watts", "hr", "cad", "speed_mps", "alt_m", "lat", "lng",
        "dist_m", "grade_pct", "temp_c", "moving",
    }  # fmt: skip
    assert "torque" not in raw.columns  # allNull stream dropped

    frame, info = icu_streams_to_parquet_frame(payload)
    assert info.n_original == 600
    assert info.resampled is True
    assert info.n_samples == 780  # t 0..779 inclusive
    assert frame["t_s"].to_list() == list(range(780))
    assert info.resolution_label == "1s"  # median step is 1 s even though gaps exist

    # The 32 s pause (t 300..330) is not invented: watts/hr null, moving False.
    pause = frame.filter((pl.col("t_s") >= 300) & (pl.col("t_s") <= 330))
    assert pause.height == 31
    assert pause["watts"].null_count() == 31
    assert pause["hr"].null_count() == 31
    assert pause["moving"].to_list() == [False] * 31

    # The 2 s-spaced block (t 331..629) is interpolated: no nulls remain.
    block = frame.filter((pl.col("t_s") >= 331) & (pl.col("t_s") <= 629))
    assert block["watts"].null_count() == 0
    assert block["dist_m"].null_count() == 0
    assert block["moving"].all()
    # Interpolated watt at t=332 sits between its neighbours at 331 and 333.
    w331, w332, w333 = frame.filter(pl.col("t_s").is_in([331, 332, 333]))["watts"].to_list()
    assert min(w331, w333) <= w332 <= max(w331, w333)

    # Regression: icu's real latlng shape (lat in data, lng in data2) must not yield nulls.
    assert raw["lat"].null_count() == 0
    assert raw["lng"].null_count() == 0
    assert raw["lat"][0] == pytest.approx(25.03307)
    assert raw["lng"][0] == pytest.approx(121.565426)

    conformed = conform(frame)  # must satisfy the StreamStore schema
    assert conformed.schema["watts"] == pl.Int16
    assert conformed.schema["lat"] == pl.Float64
    assert conformed.schema["moving"] == pl.Boolean


def test_already_1hz_is_untouched() -> None:
    raw = pl.DataFrame({"t_s": [10, 11, 12, 13], "watts": [100, 110, 120, 130]})
    out, info = resample_1hz(raw)
    assert info.resampled is False
    assert info.resolution_label == "1s"
    assert out["t_s"].to_list() == [0, 1, 2, 3]  # rebased to start at 0
    assert out["watts"].to_list() == [100, 110, 120, 130]


def test_duplicate_timestamps_keep_last_and_long_gap_stays_null() -> None:
    raw = pl.DataFrame(
        {
            "t_s": [0, 1, 1, 2, 30],
            "watts": [100, 110, 115, 120, 200],
            "hr": [120, 121, 122, 123, 150],
        }
    )
    out, info = resample_1hz(raw, max_gap_s=10)
    assert info.n_original == 4
    assert out.height == 31
    assert out.filter(pl.col("t_s") == 1)["watts"].item() == 115
    assert out.filter(pl.col("t_s") == 15)["watts"].item() is None
    assert out.filter(pl.col("t_s") == 30)["watts"].item() == 200
    assert info.resolution_label == "1s"


def test_short_gap_interpolated_and_resolution_label() -> None:
    raw = pl.DataFrame({"t_s": [0, 5, 10], "watts": [100, 200, 300], "cad": [80, 90, 100]})
    out, info = resample_1hz(raw, max_gap_s=10)
    assert out["watts"].to_list() == [100, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300]
    assert info.resolution_label == "5s"


def test_missing_time_stream_rejected() -> None:
    with pytest.raises(SchemaError, match="time"):
        streams_to_frame([{"type": "watts", "data": [1, 2, 3]}])


def test_length_mismatch_rejected() -> None:
    with pytest.raises(SchemaError, match="samples"):
        streams_to_frame([{"type": "time", "data": [0, 1, 2]}, {"type": "watts", "data": [1]}])


def test_unknown_streams_ignored_and_latlng_split() -> None:
    frame = streams_to_frame(
        [
            {"type": "time", "data": [0, 1]},
            {"type": "latlng", "data": [25.0, 25.1], "data2": [121.5, 121.6]},
            {"type": "left_right_balance", "data": [50, 51]},
        ]
    )
    assert frame.columns == ["t_s", "lat", "lng"]
    assert frame["lat"].to_list() == [25.0, 25.1]
    assert frame["lng"].to_list() == [121.5, 121.6]


def test_latlng_icu_data_data2_shape_regression() -> None:
    """icu sends lat in ``data`` and lng in ``data2``; this used to produce all-null columns."""
    payload = [
        {"type": "time", "name": "time", "data": [0, 1, 2], "data2": None},
        {
            "type": "latlng",
            "name": None,
            "data": [25.067112, 25.06711, None],
            "data2": [121.37941, 121.37941, None],
            "valueType": "java.lang.Float",
            "valueTypeIsArray": False,
            "allNull": False,
        },
    ]
    frame, _ = icu_streams_to_parquet_frame(payload)
    conformed = conform(frame)
    assert conformed["lat"].to_list()[:2] == pytest.approx([25.067112, 25.06711])
    assert conformed["lng"].to_list()[:2] == pytest.approx([121.37941, 121.37941])
    assert conformed["lat"].null_count() == 1  # genuine gaps (no fix) stay null


def test_latlng_pair_shape_still_accepted() -> None:
    frame = streams_to_frame(
        [
            {"type": "time", "data": [0, 1]},
            {"type": "latlng", "data": [[25.0, 121.5], None]},
        ]
    )
    assert frame["lat"].to_list() == [25.0, None]
    assert frame["lng"].to_list() == [121.5, None]
