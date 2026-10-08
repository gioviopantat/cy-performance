"""In-process background jobs for the API (one worker: SQLite has one writer)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from cyp.core.timeutil import iso_utc, now_utc
from cyp.logging import get_logger
from cyp.schemas import JobOut

log = get_logger(__name__)
MAX_KEPT = 50


class JobManager:
    """Queue functions, keep their status/result for polling (``GET /v1/jobs/{id}``)."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cyp-job")
        self._jobs: dict[str, JobOut] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[], Any], *, profile: str = "") -> JobOut:
        """Queue ``fn`` for ``profile``; returns the queued job."""
        job = JobOut(id=uuid.uuid4().hex[:12], kind=kind, status="queued", profile=profile)
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > MAX_KEPT:
                for old in list(self._jobs)[: len(self._jobs) - MAX_KEPT]:
                    if self._jobs[old].status in ("ok", "failed"):
                        del self._jobs[old]
        self._pool.submit(self._run, job.id, fn)
        return job

    def _run(self, job_id: str, fn: Callable[[], Any]) -> None:
        self._update(job_id, status="running", started_at=iso_utc(now_utc()))
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - reported to the client, not swallowed
            log.error("job.failed", job_id=job_id, error=str(exc))
            self._update(
                job_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=iso_utc(now_utc()),
            )
            return
        payload = result if isinstance(result, dict) else {"result": result}
        self._update(job_id, status="ok", result=payload, finished_at=iso_utc(now_utc()))

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._jobs[job_id] = job.model_copy(update=values)

    def get(self, job_id: str) -> JobOut | None:
        """Current state of a job."""
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, profile: str | None = None) -> list[JobOut]:
        """Kept jobs (of ``profile`` when given), newest last."""
        with self._lock:
            return [j for j in self._jobs.values() if profile is None or j.profile == profile]

    def wait(self, timeout: float = 30.0) -> None:
        """Block until the queue is drained (tests)."""
        self._pool.submit(lambda: None).result(timeout=timeout)

    def shutdown(self) -> None:
        """Stop the worker."""
        self._pool.shutdown(wait=False, cancel_futures=True)
