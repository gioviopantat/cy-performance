"""Identity of the calendar events we own (ADR-0005), shared by planning and publishing.

``external_id = cyp:{season}:{YYYY-MM-DD}:{slot}``: the planner stores it on planned workouts,
the publisher matches calendar events on it. Pure; no I/O.
"""

from __future__ import annotations

import datetime as dt
import re

PREFIX = "cyp:"
TAG = "cyp"
_EXTERNAL_RE = re.compile(r"^cyp:(?P<season>[^:]+):(?P<date>\d{4}-\d{2}-\d{2}):(?P<slot>\d+)$")


def external_id(season_key: str, day: dt.date, slot: int = 1) -> str:
    """``cyp:{season}:{YYYY-MM-DD}:{slot}``."""
    if ":" in season_key or not season_key:
        raise ValueError(f"season key must be non-empty and colon-free: {season_key!r}")
    return f"{PREFIX}{season_key}:{day.isoformat()}:{slot}"


def parse_external_id(value: str | None) -> tuple[str, dt.date, int] | None:
    """Inverse of :func:`external_id`; ``None`` for foreign / malformed ids."""
    if not value:
        return None
    m = _EXTERNAL_RE.match(value)
    if m is None:
        return None
    return m["season"], dt.date.fromisoformat(m["date"]), int(m["slot"])
