"""Our calendar events: ``external_id`` scheme and the icu ``EventEx`` payload (ADR-0005)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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


def is_ours(event: dict[str, Any]) -> bool:
    """True for events we created (``external_id`` with our prefix). Others are read-only."""
    ext = event.get("external_id")
    return isinstance(ext, str) and ext.startswith(PREFIX)


class EventSpec(BaseModel):
    """Desired state of one calendar event we own."""

    model_config = ConfigDict(extra="forbid")

    external_id: str
    date: dt.date
    name: str
    description: str = ""
    category: Literal["WORKOUT", "NOTE"] = "WORKOUT"
    type: str = "Ride"
    indoor: bool = False
    target: Literal["POWER", "HR", "PACE", "AUTO"] = "POWER"
    moving_time_s: int | None = None
    target_load: float | None = Field(default=None, ge=0)
    tags: list[str] = Field(default_factory=list)
    color: str | None = None

    def payload(self, *, uid: bool = False) -> dict[str, Any]:
        """The icu ``EventEx`` JSON; ``uid=True`` also sets ``uid = external_id`` (upsertOnUid)."""
        out: dict[str, Any] = {
            "category": self.category,
            "start_date_local": f"{self.date.isoformat()}T00:00:00",
            "name": self.name,
            "description": self.description,
            "external_id": self.external_id,
            "tags": sorted({TAG, *self.tags}),
        }
        if self.category == "WORKOUT":
            out.update({"type": self.type, "indoor": self.indoor, "target": self.target})
            if self.moving_time_s is not None:
                out["moving_time"] = self.moving_time_s
        if self.color:
            out["color"] = self.color
        if uid:
            out["uid"] = self.external_id
        return out

    def fingerprint(self) -> tuple[Any, ...]:
        """Fields compared against the read-back to decide update vs noop."""
        return (
            self.name,
            self.description.strip(),
            self.category,
            self.indoor if self.category == "WORKOUT" else None,
            self.moving_time_s if self.category == "WORKOUT" else None,
        )


def remote_fingerprint(event: dict[str, Any]) -> tuple[Any, ...]:
    """Same tuple as :meth:`EventSpec.fingerprint` from an icu event."""
    cat = event.get("category")
    return (
        event.get("name"),
        str(event.get("description") or "").strip(),
        cat,
        bool(event.get("indoor")) if cat == "WORKOUT" else None,
        event.get("moving_time") if cat == "WORKOUT" else None,
    )
