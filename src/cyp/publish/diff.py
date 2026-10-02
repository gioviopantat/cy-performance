"""Desired vs current calendar -> minimal create / update / delete set (ADR-0005). Pure.

Rules:
- Only events whose ``external_id`` starts with ``cyp:`` are ours; everything else is ignored.
- Mutability window: past days are never touched; today only while ``now_local`` is before
  the cut-off; future days freely.
- An external_id we published before (``previously_published``) that is now missing from the
  calendar was deleted by the athlete: it is *declined*, never recreated.
- At most ``max_events`` writes (creates + updates + deletes) per run; the rest is deferred.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from cyp.publish.events import EventSpec, is_ours, parse_external_id, remote_fingerprint

MAX_EVENTS_PER_RUN = 20


@dataclass
class Diff:
    """What a publish run would do."""

    create: list[EventSpec] = field(default_factory=list)
    update: list[tuple[EventSpec, dict[str, Any]]] = field(default_factory=list)
    delete: list[dict[str, Any]] = field(default_factory=list)
    noop: list[EventSpec] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)  # external_ids outside the window
    declined: list[str] = field(default_factory=list)  # deleted by the athlete
    deferred: list[str] = field(default_factory=list)  # over max_events

    @property
    def n_writes(self) -> int:
        """Creates + updates + deletes."""
        return len(self.create) + len(self.update) + len(self.delete)

    def summary(self) -> dict[str, int]:
        """Counts per bucket."""
        return {
            "create": len(self.create),
            "update": len(self.update),
            "delete": len(self.delete),
            "noop": len(self.noop),
            "frozen": len(self.frozen),
            "declined": len(self.declined),
            "deferred": len(self.deferred),
        }


def mutable(day: dt.date, now_local: dt.datetime, cutoff: dt.time) -> bool:
    """Can an event on ``day`` still be changed at ``now_local``?"""
    today = now_local.date()
    if day < today:
        return False
    if day == today:
        return now_local.time() < cutoff
    return True


def _event_day(event: dict[str, Any]) -> dt.date | None:
    parsed = parse_external_id(event.get("external_id"))
    if parsed:
        return parsed[1]
    raw = str(event.get("start_date_local") or "")[:10]
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def compute_diff(
    desired: Sequence[EventSpec],
    current: Iterable[dict[str, Any]],
    *,
    window: tuple[dt.date, dt.date],
    now_local: dt.datetime,
    cutoff: dt.time = dt.time(10, 0),
    previously_published: Iterable[str] = (),
    max_events: int = MAX_EVENTS_PER_RUN,
) -> Diff:
    """Compare ``desired`` (our plan inside ``window``) with the calendar read-back."""
    lo, hi = window
    ours = {e["external_id"]: e for e in current if is_ours(e)}
    published = set(previously_published)
    diff = Diff()
    wanted = {d.external_id for d in desired}
    budget = max_events

    def take(ext: str) -> bool:
        nonlocal budget
        if budget <= 0:
            diff.deferred.append(ext)
            return False
        budget -= 1
        return True

    for spec in sorted(desired, key=lambda d: (d.date, d.external_id)):
        if not (lo <= spec.date <= hi):
            continue
        remote = ours.get(spec.external_id)
        if not mutable(spec.date, now_local, cutoff):
            diff.frozen.append(spec.external_id)
            continue
        if remote is None:
            if spec.external_id in published:
                diff.declined.append(spec.external_id)
            elif take(spec.external_id):
                diff.create.append(spec)
        elif remote_fingerprint(remote) == spec.fingerprint():
            diff.noop.append(spec)
        elif take(spec.external_id):
            diff.update.append((spec, remote))

    for ext, remote in sorted(ours.items()):
        day = _event_day(remote)
        if ext in wanted or day is None or not (lo <= day <= hi):
            continue
        if not mutable(day, now_local, cutoff):
            diff.frozen.append(ext)
        elif take(ext):
            diff.delete.append(remote)
    return diff
