"""``job_run`` context manager: one ``job_runs`` row per stage execution (docs/01 §8)."""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from cyp.logging import get_logger
from cyp.store.repo.job_runs import JobRunRepo

log = get_logger(__name__)


@dataclass
class RunContext:
    """Mutable handle the job body uses to report counts and rate-limit snapshots."""

    run_id: int
    job: str
    counts: dict[str, int] = field(default_factory=dict)
    rate_limit_snapshot: dict[str, Any] | None = None

    def incr(self, key: str, by: int = 1) -> None:
        """Increment a counter (created at 0 if missing)."""
        self.counts[key] = self.counts.get(key, 0) + by


@contextmanager
def job_run(
    name: str,
    factory: sessionmaker[Session],
    *,
    log_path: str | None = None,
) -> Iterator[RunContext]:
    """Record a ``job_runs`` row around the enclosed block.

    The row is committed as ``running`` on entry so a crash leaves a visible trace, then
    updated to ``ok`` or ``failed`` (with the traceback in ``error``) on exit. Exceptions
    propagate after being recorded.
    """
    with factory() as session:
        run = JobRunRepo(session).start(name, log_path=log_path)
        session.commit()
        run_id = run.id
    ctx = RunContext(run_id=run_id, job=name)
    bound = log.bind(job=name, run_id=run_id)
    bound.info("job.start")
    try:
        yield ctx
    except BaseException as exc:
        _finish(factory, ctx, status="failed", error=_format_error(exc))
        bound.error("job.failed", error=str(exc), counts=ctx.counts)
        raise
    else:
        _finish(factory, ctx, status="ok", error=None)
        bound.info("job.ok", counts=ctx.counts)


def _finish(
    factory: sessionmaker[Session], ctx: RunContext, *, status: str, error: str | None
) -> None:
    with factory() as session:
        repo = JobRunRepo(session)
        run = repo.get(ctx.run_id)
        if run is not None:
            repo.finish(
                run,
                status=status,
                counts=dict(ctx.counts),
                error=error,
                rate_limit_snapshot=ctx.rate_limit_snapshot,
            )
        session.commit()


def _format_error(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(exc))
    return text[-4000:]
