"""Background jobs: sync, analyze, daily, weekly."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from cyp.api.deps import ctx, jobs
from cyp.jobs.runner import profile_lock
from cyp.schemas import JobOut
from cyp.services import pipeline
from cyp.services.context import AppContext
from cyp.services.jobs import JobManager

router = APIRouter(prefix="/jobs", tags=["jobs"])
Ctx = Annotated[AppContext, Depends(ctx)]
Jobs = Annotated[JobManager, Depends(jobs)]
Kind = Literal["sync", "analyze", "daily", "weekly"]


@router.post("/{kind}", response_model=JobOut, status_code=202)
def start(c: Ctx, j: Jobs, kind: Kind) -> JobOut:
    """Queue a job (one runs at a time); poll ``GET /jobs/{id}``."""
    if kind == "sync" and not c.settings.intervals_api_key.get_secret_value():
        raise HTTPException(status_code=409, detail="INTERVALS_API_KEY is not set")
    fn: Callable[[], object] = {
        "sync": lambda: pipeline.run_sync_job(c),
        "analyze": lambda: pipeline.analyze(c),
        "daily": lambda: pipeline.daily(
            c, sync=bool(c.settings.intervals_api_key.get_secret_value())
        ),
        "weekly": lambda: pipeline.weekly(c),
    }[kind]

    def locked() -> object:
        # The autopilot (launchd or the UI) holds this lock while it runs: never interleave.
        with profile_lock(c.settings.cyp_data_dir):
            return fn()

    return j.submit(kind, locked, profile=c.settings.cyp_profile)


@router.get("", response_model=list[JobOut])
def list_jobs(c: Ctx, j: Jobs) -> list[JobOut]:
    """This profile's jobs started by this server process."""
    return j.list(profile=c.settings.cyp_profile)


@router.get("/{job_id}", response_model=JobOut)
def get_job(j: Jobs, job_id: str) -> JobOut:
    """Status / result of one job."""
    job = j.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return job
