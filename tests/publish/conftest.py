"""In-memory intervals.icu calendar that can emulate each bulk-upsert behaviour."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

Honours = Literal["external_id", "uid", "none"]


class FakeCalendar:
    """Implements the :class:`CalendarClient` protocol over a list of event dicts.

    ``honours`` decides what ``events/bulk`` matches on: ``external_id`` (``upsert=true``
    works), ``uid`` (only ``upsertOnUid`` works) or ``none`` (every post creates a copy).
    ``load_per_min`` is how icu "computes" ``icu_training_load`` from ``moving_time``.
    """

    def __init__(self, honours: Honours = "external_id", load_per_min: float = 1.0) -> None:
        self.honours = honours
        self.load_per_min = load_per_min
        self.events: list[dict[str, Any]] = []
        self.next_id = 1000
        self.writes: list[tuple[str, Any]] = []

    def add(self, **event: Any) -> dict[str, Any]:
        event.setdefault("id", self._id())
        self.events.append(event)
        return event

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def list_events(
        self, oldest: Any, newest: Any | None = None, *, category: Any = None
    ) -> list[dict[str, Any]]:
        lo = str(oldest)[:10]
        hi = str(newest or "9999-12-31")[:10]
        return [dict(e) for e in self.events if lo <= e["start_date_local"][:10] <= hi]

    def bulk_upsert_events(
        self, events: list[dict[str, Any]], *, mode: Literal["upsert", "uid"] = "upsert"
    ) -> list[dict[str, Any]]:
        self.writes.append((f"bulk:{mode}", events))
        out = []
        for payload in events:
            key = None
            if mode == "upsert" and self.honours == "external_id":
                key = ("external_id", payload.get("external_id"))
            if mode == "uid" and self.honours == "uid":
                key = ("uid", payload.get("uid"))
            match = next((e for e in self.events if key and e.get(key[0]) == key[1]), None)
            ev = {**payload}
            if ev.get("moving_time"):
                ev["icu_training_load"] = round(ev["moving_time"] / 60 * self.load_per_min, 1)
            if match is not None:
                match.update(ev)
                out.append(dict(match))
            else:
                out.append(self.add(**ev))
        return out

    def bulk_delete_events(self, refs: list[dict[str, Any]]) -> Any:
        self.writes.append(("delete", refs))
        ids = {r.get("id") for r in refs if "id" in r}
        exts = {r.get("external_id") for r in refs if "external_id" in r}
        self.events = [
            e for e in self.events if e.get("id") not in ids and e.get("external_id") not in exts
        ]
        return {"eventsDeleted": len(refs)}


def day(offset: int, base: dt.date = dt.date(2026, 10, 2)) -> dt.date:
    return base + dt.timedelta(days=offset)
