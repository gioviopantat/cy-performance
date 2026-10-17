"""Run the autopilot from the UI; read back recent runs (spec web-ui).

The only calendar write the HTTP API offers: ``write=true`` needs ``confirm=true``, the
profile in ``planner.mode: apply``, flag ``api.calendar_write`` and the write guard inside
``publish_plan`` (via ``jobs.runner.run_profile``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from cyp.api.deps import ctx, jobs
from cyp.schemas import AutopilotRequest, AutopilotRunOut, JobOut
from cyp.services.autopilot import recent_runs
from cyp.services.context import AppContext
from cyp.services.jobs import JobManager

router = APIRouter(prefix="/autopilot", tags=["autopilot"])
Ctx = Annotated[AppContext, Depends(ctx)]
Jobs = Annotated[JobManager, Depends(jobs)]


@router.post("", response_model=JobOut, status_code=202)
def run(c: Ctx, j: Jobs, body: AutopilotRequest) -> JobOut:
    """Queue this profile's morning run; poll ``GET /v1/jobs/{id}`` for the report."""
    from cyp.jobs.runner import run_profile

    if body.write:
        if not body.confirm:
            raise HTTPException(status_code=422, detail="write=true needs confirm=true")
        if c.raw_athlete_config().planner.mode != "apply":
            raise HTTPException(status_code=409, detail="profile is in propose mode")
        if not c.features().enabled("api.calendar_write"):
            raise HTTPException(status_code=403, detail="feature api.calendar_write is off")
    settings = c.settings
    kind = "autopilot-write" if body.write else "autopilot"
    return j.submit(
        kind,
        lambda: run_profile(settings, no_write=not body.write).to_dict(),
        profile=settings.cyp_profile,
    )


@router.get("/runs", response_model=list[AutopilotRunOut])
def runs(c: Ctx, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> list[AutopilotRunOut]:
    """Saved run reports (scheduled and manual), newest first."""
    return [AutopilotRunOut.model_validate(r) for r in recent_runs(c, limit)]
