"""Explanation / Reason / MethodRef models (docs/07 §1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cyp.core.explain import Explanation, MethodRef, Reason


def _sample() -> Explanation:
    return Explanation(
        key="readiness.verdict",
        headline_zh="今天狀態普通，照表操課。",
        because=[
            Reason(
                text_zh="HRV 比 7 日均值低 0.8 個標準差",
                evidence={"hrv": 52, "hrv_mean_7d": 58, "z": -0.8},
                weight=0.5,
            ),
            Reason(text_zh="TSB -12 在可接受範圍", evidence={"tsb": -12}, weight=0.3),
        ],
        method=MethodRef(
            model_id="readiness_v1",
            version="1.0",
            inputs={"hrv_z": -0.8, "tsb": -12},
            doc="docs/glossary/readiness_v1.md",
        ),
        confidence="medium",
        glossary_terms=["readiness_v1", "banister_pmc"],
    )


def test_json_roundtrip() -> None:
    e = _sample()
    blob = e.to_json_dict()
    assert blob["key"] == "readiness.verdict"
    assert blob["because"][0]["evidence"]["z"] == -0.8
    assert Explanation.from_json_dict(blob) == e


def test_confidence_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        Explanation(key="k", headline_zh="x", confidence="certain")  # type: ignore[arg-type]


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Reason(text_zh="x", evidence={}, bogus=1)  # type: ignore[call-arg]


def test_method_optional_and_lists_default_empty() -> None:
    e = Explanation(key="k", headline_zh="x", confidence="low")
    assert e.method is None
    assert e.because == []
    assert e.glossary_terms == []
