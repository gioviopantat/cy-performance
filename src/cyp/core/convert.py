"""Lenient conversions for loosely-typed API / JSON values. Pure."""

from __future__ import annotations

from typing import Any


def as_float(value: Any) -> float | None:
    """``float(value)``; ``None`` for ``None``, booleans and unparsable values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None
