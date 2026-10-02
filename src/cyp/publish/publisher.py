"""Diff-first, idempotent publishing of our events to intervals.icu (ADR-0005, docs/02 §4).

Flow per run: read back the window -> :func:`compute_diff` -> (``propose``: stop and return the
diff) -> ``bulk`` upsert of creates+updates -> ``bulk-delete`` of stale ones -> read back ->
verify each written WORKOUT's ``icu_training_load`` within ``tolerance`` of its target ->
``publish_log`` rows (``ok`` / ``needs_review`` / ``failed``).

Never writes in ``propose`` mode. ``apply`` additionally requires ``allow_write=True`` from the
caller (the CLI only passes it with an explicit flag), so a mis-set environment variable alone
cannot write to the athlete's calendar.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker

from cyp.core.errors import IngestError
from cyp.core.timeutil import iso_utc, now_utc
from cyp.logging import get_logger
from cyp.publish.diff import Diff, compute_diff
from cyp.publish.events import EventSpec, is_ours
from cyp.store.models import PublishLog

log = get_logger(__name__)

UpsertMode = Literal["upsert", "uid"]
LOAD_TOLERANCE = 0.10


class CalendarClient(Protocol):
    """The slice of :class:`IntervalsClient` the publisher needs."""

    def list_events(
        self, oldest: Any, newest: Any | None = None, *, category: Any = None
    ) -> list[dict[str, Any]]:
        """Read events in an inclusive local-date window."""
        ...

    def bulk_upsert_events(
        self, events: list[dict[str, Any]], *, mode: UpsertMode = "upsert"
    ) -> list[dict[str, Any]]:
        """Create or update events in one call."""
        ...

    def bulk_delete_events(self, refs: list[dict[str, Any]]) -> Any:
        """Delete events by id / external_id."""
        ...


@dataclass
class PublishResult:
    """Outcome of one run."""

    mode: Literal["propose", "apply"]
    diff: Diff
    written: int = 0
    deleted: int = 0
    verified: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)
    failed: str | None = None


def verify_load(target: float | None, actual: float | None, tolerance: float) -> bool | None:
    """``True`` within tolerance, ``False`` outside, ``None`` when there is nothing to compare."""
    if target is None or actual is None or target <= 0:
        return None
    return abs(actual - target) / target <= tolerance


class Publisher:
    """Publish a window of desired events."""

    def __init__(
        self,
        client: CalendarClient,
        factory: sessionmaker[Session] | None = None,
        *,
        upsert_mode: UpsertMode = "upsert",
        tolerance: float = LOAD_TOLERANCE,
    ) -> None:
        self.client = client
        self.factory = factory
        self.upsert_mode = upsert_mode
        self.tolerance = tolerance

    def read_back(self, window: tuple[dt.date, dt.date]) -> list[dict[str, Any]]:
        """Every calendar event in the window (ours and the athlete's)."""
        return list(self.client.list_events(window[0], window[1]))

    def run(
        self,
        desired: Sequence[EventSpec],
        *,
        window: tuple[dt.date, dt.date],
        now_local: dt.datetime,
        mode: Literal["propose", "apply"] = "propose",
        allow_write: bool = False,
        cutoff: dt.time = dt.time(10, 0),
        previously_published: Sequence[str] = (),
        run_id: int | None = None,
    ) -> PublishResult:
        """Compute the diff and, in ``apply`` mode with ``allow_write``, execute and verify it."""
        current = self.read_back(window)
        diff = compute_diff(
            desired,
            current,
            window=window,
            now_local=now_local,
            cutoff=cutoff,
            previously_published=previously_published,
        )
        result = PublishResult(mode=mode, diff=diff)
        if mode != "apply" or not allow_write or diff.n_writes == 0:
            return result

        uid = self.upsert_mode == "uid"
        specs = [*diff.create, *(spec for spec, _ in diff.update)]
        logs: list[dict[str, Any]] = []
        try:
            if specs:
                self.client.bulk_upsert_events(
                    [s.payload(uid=uid) for s in specs], mode=self.upsert_mode
                )
                result.written = len(specs)
            if diff.delete:
                refs = [
                    {"id": e["id"]}
                    if e.get("id") is not None
                    else {"external_id": e["external_id"]}
                    for e in diff.delete
                ]
                self.client.bulk_delete_events(refs)
                result.deleted = len(refs)
        except IngestError as exc:
            result.failed = str(exc)
            log.error("publish.failed", error=str(exc))
            for s in specs:
                logs.append(self._log(run_id, "create", s.external_id, None, s.payload(), "failed"))
            self._persist(logs)
            return result

        after = {e["external_id"]: e for e in self.read_back(window) if is_ours(e)}
        create_ids = {s.external_id for s in diff.create}
        for s in specs:
            remote = after.get(s.external_id)
            action = "create" if s.external_id in create_ids else "update"
            if remote is None:
                result.needs_review.append(s.external_id)
                logs.append(
                    self._log(run_id, action, s.external_id, None, s.payload(), "needs_review")
                )
                continue
            ok = verify_load(s.target_load, remote.get("icu_training_load"), self.tolerance)
            status = "needs_review" if ok is False else "ok"
            (result.needs_review if ok is False else result.verified).append(s.external_id)
            logs.append(
                self._log(
                    run_id,
                    action,
                    s.external_id,
                    remote,
                    s.payload(),
                    status,
                    remote.get("icu_training_load"),
                )
            )
        for e in diff.delete:
            logs.append(self._log(run_id, "delete", e.get("external_id"), e, None, "ok"))
        self._persist(logs)
        return result

    @staticmethod
    def _log(
        run_id: int | None,
        action: str,
        ext: str | None,
        remote: dict[str, Any] | None,
        request: dict[str, Any] | None,
        status: str,
        load: float | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "action": action,
            "external_id": ext,
            "icu_event_id": remote.get("id") if remote else None,
            "request": request,
            "response": remote,
            "status": status,
            "verified_load": load,
            "at": iso_utc(now_utc()),
        }

    def _persist(self, rows: list[dict[str, Any]]) -> None:
        if self.factory is None or not rows:
            return
        with self.factory() as s:
            for r in rows:
                s.add(PublishLog(**r))
            s.commit()
