"""M3 spike: find the bulk-upsert mode intervals.icu honours for *this* auth (ADR-0005).

The OpenAPI spec ties ``upsert=true`` (match on ``external_id``) to OAuth applications; with a
personal API key it may silently create duplicates instead. The spike writes one clearly named
throw-away WORKOUT on a far-future date, posts it twice per mode, reads the day back, counts the
copies and deletes everything it created (by id and by external_id):

1. ``upsert=true`` twice with the same ``external_id`` -> 1 copy = supported.
2. else ``upsertOnUid=true`` twice with ``uid = external_id`` -> 1 copy = supported.

Result ``"upsert" | "uid" | "none"`` is what :class:`~cyp.publish.publisher.Publisher` should
use. It writes to the athlete's real calendar, so the CLI only runs it with an explicit
confirmation flag; :func:`plan_spike` describes the writes without doing them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

from cyp.core.errors import IngestError
from cyp.publish.events import EventSpec
from cyp.publish.publisher import CalendarClient

SPIKE_NAME = "cyp spike — 測試事件，可刪除"
SPIKE_SEASON = "spike"


def spike_spec(day: dt.date, *, version: int, mode: str) -> EventSpec:
    """The throw-away event (``version`` changes the description so the 2nd post is an update)."""
    return EventSpec(
        external_id=f"cyp:{SPIKE_SEASON}-{mode}:{day.isoformat()}:1",
        date=day,
        name=SPIKE_NAME,
        description=f"- 10m 50%\n\n(cyp M3 upsert spike, mode={mode}, write #{version})",
        moving_time_s=600,
        tags=["spike"],
    )


@dataclass
class SpikeResult:
    """What the spike observed."""

    day: dt.date
    supported: Literal["upsert", "uid", "none"]
    copies: dict[str, int] = field(default_factory=dict)
    cleaned_up: int = 0
    leftovers: list[Any] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def plan_spike(day: dt.date) -> list[str]:
    """Human-readable list of the writes :func:`run_spike` would make."""
    ext_upsert = spike_spec(day, version=1, mode="upsert").external_id
    ext_uid = spike_spec(day, version=1, mode="uid").external_id
    return [
        f"POST events/bulk?upsert=true ×2 → {ext_upsert}",
        f"POST events/bulk?upsertOnUid=true ×2 → {ext_uid}（只有第一種不支援時才做）",
        f"GET events {day.isoformat()} 計算副本數",
        "PUT events/bulk-delete 刪除上面建立的所有事件（含重複副本）",
    ]


def _ours_on(client: CalendarClient, day: dt.date, ext: str) -> list[dict[str, Any]]:
    return [
        e
        for e in client.list_events(day, day)
        if e.get("external_id") == ext or e.get("uid") == ext
    ]


def _try_mode(client: CalendarClient, day: dt.date, mode: Literal["upsert", "uid"]) -> int:
    for version in (1, 2):
        spec = spike_spec(day, version=version, mode=mode)
        client.bulk_upsert_events([spec.payload(uid=mode == "uid")], mode=mode)
    return len(_ours_on(client, day, spike_spec(day, version=1, mode=mode).external_id))


def run_spike(client: CalendarClient, day: dt.date) -> SpikeResult:
    """Run the spike against the live calendar on ``day`` and clean up after itself.

    Raises:
        ValueError: ``day`` is not in the future (we never write to past/today).
    """
    if day <= dt.date.today():
        raise ValueError("spike day must be in the future")
    result = SpikeResult(day=day, supported="none")
    try:
        n = _try_mode(client, day, "upsert")
        result.copies["upsert"] = n
        if n == 1:
            result.supported = "upsert"
        else:
            result.notes.append(
                f"upsert=true 產生 {n} 個副本：此認證方式不支援以 external_id upsert"
            )
            n_uid = _try_mode(client, day, "uid")
            result.copies["uid"] = n_uid
            if n_uid == 1:
                result.supported = "uid"
            else:
                result.notes.append(f"upsertOnUid=true 也產生 {n_uid} 個副本")
    finally:
        result.cleaned_up, result.leftovers = _cleanup(client, day)
    return result


def _cleanup(client: CalendarClient, day: dt.date) -> tuple[int, list[Any]]:
    exts = {spike_spec(day, version=1, mode=m).external_id for m in ("upsert", "uid")}
    mine = [
        e
        for e in client.list_events(day, day)
        if e.get("external_id") in exts or e.get("uid") in exts or e.get("name") == SPIKE_NAME
    ]
    refs = [{"id": e["id"]} for e in mine if e.get("id") is not None]
    if refs:
        with contextlib.suppress(IngestError):  # leftovers are reported below
            client.bulk_delete_events(refs)
    left = [
        e.get("id")
        for e in client.list_events(day, day)
        if e.get("external_id") in exts or e.get("name") == SPIKE_NAME
    ]
    return len(refs), left
