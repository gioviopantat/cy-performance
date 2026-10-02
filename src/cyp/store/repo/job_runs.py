"""``job_runs`` repository."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import JobRun
from cyp.store.repo.base import Repo


class JobRunRepo(Repo):
    """Create and finish ``job_runs`` rows; query the latest per job."""

    def start(self, job: str, *, log_path: str | None = None) -> JobRun:
        """Insert a ``running`` row and flush so the id is available."""
        run = JobRun(
            job=job,
            started_at=iso_utc(now_utc()),
            status="running",
            counts={},
            log_path=log_path,
        )
        self.session.add(run)
        self.flush()
        return run

    def finish(
        self,
        run: JobRun,
        *,
        status: str,
        counts: dict[str, Any] | None = None,
        error: str | None = None,
        rate_limit_snapshot: dict[str, Any] | None = None,
    ) -> JobRun:
        """Mark the run finished with its outcome."""
        run.finished_at = iso_utc(now_utc())
        run.status = status
        if counts is not None:
            run.counts = counts
        run.error = error
        if rate_limit_snapshot is not None:
            run.rate_limit_snapshot = rate_limit_snapshot
        self.flush()
        return run

    def get(self, run_id: int) -> JobRun | None:
        """Fetch by id."""
        return self.session.get(JobRun, run_id)

    def latest(self, job: str) -> JobRun | None:
        """Most recent run of ``job`` by start time."""
        stmt = select(JobRun).where(JobRun.job == job).order_by(JobRun.started_at.desc()).limit(1)
        return self.session.scalars(stmt).first()

    def latest_per_job(self) -> dict[str, JobRun]:
        """Mapping job name -> most recent run."""
        stmt = select(JobRun).order_by(JobRun.started_at.desc(), JobRun.id.desc())
        out: dict[str, JobRun] = {}
        for run in self.session.scalars(stmt):
            out.setdefault(run.job, run)
        return out

    def list_for(self, job: str, limit: int = 20) -> list[JobRun]:
        """Recent runs of ``job``, newest first."""
        stmt = (
            select(JobRun).where(JobRun.job == job).order_by(JobRun.started_at.desc()).limit(limit)
        )
        return list(self.session.scalars(stmt))
