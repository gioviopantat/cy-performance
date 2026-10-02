"""Explainability primitives (docs/07-explainability.md §1).

Every non-trivial result can carry an :class:`Explanation` describing *what* was concluded,
*why* (ordered :class:`Reason` list with the exact numbers quoted) and *how* (:class:`MethodRef`).
Explanations are persisted as JSON next to the result; renderers read only persisted objects.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class Reason(BaseModel):
    """One factor behind a conclusion, most important first in ``Explanation.because``."""

    model_config = ConfigDict(extra="forbid")

    text_zh: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    weight: float | None = None


class MethodRef(BaseModel):
    """Which model/algorithm produced the result, with the inputs actually used."""

    model_config = ConfigDict(extra="forbid")

    model_id: str
    version: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    doc: str


class Explanation(BaseModel):
    """Human-readable, machine-checkable justification of a result."""

    model_config = ConfigDict(extra="forbid")

    key: str
    headline_zh: str
    because: list[Reason] = Field(default_factory=list)
    method: MethodRef | None = None
    confidence: Confidence
    glossary_terms: list[str] = Field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-safe dict for the ``explanation`` columns."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> Explanation:
        """Inverse of :meth:`to_json_dict`."""
        return cls.model_validate(data)
