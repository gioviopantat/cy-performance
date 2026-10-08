"""Autopilot: one profile's whole morning in one call (ADR-0006 L2, docs/01 §6).

Stages, each switchable by a feature flag (ADR-0008) and isolated from the others:

    sync -> analyze (rides, trends, readiness) -> report.daily -> report.weekly (Mondays)
         -> plan (rolling horizon) -> publish (diff; writes only in planner.mode=apply)

A failed stage is recorded and the run continues where that is still meaningful (a failed sync
still plans from stored data; a failed plan skips publishing). Publishing never writes a plan
that needs review. One ``job_runs`` row (``autopilot``) per run; it is ``failed`` when any stage
failed. The caller (``cyp run``) owns locking and process isolation.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from cyp.analysis.longitudinal.run import load_latest_report
from cyp.core.errors import ConfigError, CypError
from cyp.logging import get_logger
from cyp.planning.job import PlanRun, build_plan
from cyp.services import pipeline
from cyp.services import publish as publish_service
from cyp.services import readiness as readiness_service
from cyp.services import reports as reports_service
from cyp.services import trends as trends_service
from cyp.services.context import AppContext
from cyp.store.runs import job_run

log = get_logger(__name__)

JOB = "autopilot"
Status = Literal["ok", "skipped", "failed"]


@dataclass
class StageResult:
    """Outcome of one stage."""

    name: str
    status: Status
    detail: str = ""


@dataclass
class AutopilotReport:
    """Outcome of one profile's run (printed as JSON by ``cyp run --json``)."""

    profile: str
    day: dt.date
    planner_mode: str
    stages: list[StageResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No stage failed."""
        return all(s.status != "failed" for s in self.stages)

    def stage(self, name: str) -> StageResult | None:
        """Result of stage ``name`` if it ran."""
        return next((s for s in self.stages if s.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "profile": self.profile,
            "day": self.day.isoformat(),
            "planner_mode": self.planner_mode,
            "ok": self.ok,
            "stages": [asdict(s) for s in self.stages],
        }


class _StagesFailedError(CypError):
    """Marks the ``job_runs`` row failed when any stage failed."""


def run_autopilot(
    ctx: AppContext,
    *,
    allow_write: bool = True,
    now_local: dt.datetime | None = None,
    client_factory: publish_service.ClientFactory | None = None,
) -> AutopilotReport:
    """Run every enabled stage for this context's profile (see module docstring).

    ``allow_write=False`` (``cyp run --no-write``) publishes as a diff even in apply mode.
    Never raises for a profile problem: an invalid athlete.yaml or flag is a failed ``config``
    stage, recorded in ``job_runs`` like any other failure.
    """
    now = now_local or ctx.now_local()
    today = now.date()
    report = AutopilotReport(ctx.settings.cyp_profile or "default", today, "unknown")
    try:
        cfg = ctx.athlete_config()
        features = ctx.features()
    except ConfigError as exc:
        report.stages.append(StageResult("config", "failed", str(exc).splitlines()[0]))
        record_failure(ctx, report, now)
        return report
    report.planner_mode = cfg.planner.mode

    def stage(name: str, flag: str, body: Callable[[], str]) -> StageResult:
        if not features.enabled(flag):
            result = StageResult(name, "skipped", f"feature {flag} is off")
        else:
            try:
                result = StageResult(name, "ok", body())
            except _Skip as skip:
                result = StageResult(name, "skipped", str(skip))
            except Exception as exc:
                log.exception("autopilot.stage_failed", stage=name)
                result = StageResult(name, "failed", f"{type(exc).__name__}: {exc}")
        report.stages.append(result)
        return result

    plan_holder: list[PlanRun] = []

    def do_sync() -> str:
        if not ctx.settings.intervals_api_key.get_secret_value():
            raise ConfigError("INTERVALS_API_KEY is not set")
        out = pipeline.run_sync_job(ctx)  # checks the key owner first (jobs/sync.py)
        strava = "on" if features.enabled("sync.strava") else "off"
        return f"strava {strava}; " + _brief(out)

    def do_analyze() -> str:
        counts = pipeline.analyze(ctx)
        trends = trends_service.recompute(ctx, as_of=today)
        readiness_service.recompute(ctx, [today])
        limiters = ", ".join(lim["id"] for lim in trends.get("limiters", [])) or "none"
        return f"{_brief(counts)}; limiters {limiters}"

    def do_daily() -> str:
        return reports_service.build(ctx, "daily", today).stem

    def do_weekly() -> str:
        if today.weekday() != 0:
            raise _Skip("weekly report runs on Mondays")
        return reports_service.build(ctx, "weekly", today - dt.timedelta(days=1)).stem

    def do_plan() -> str:
        bias = (load_latest_report(ctx.reports_dir) or {}).get("planner_bias", {})
        with ctx.write_lock:
            run = build_plan(ctx.factory, cfg, today=today, now_local=now, bias=bias, trigger=JOB)
        plan_holder.append(run)
        changes = ", ".join(f"{k} {len(v)}" for k, v in run.changes.items()) or "no changes"
        review = f"; NEEDS REVIEW ({len(run.violations)})" if run.needs_review else ""
        return f"{len(run.days)} days, {changes}{review}"

    def do_publish() -> str:
        if not plan_holder:
            raise _Skip("no plan this run")
        run = plan_holder[0]
        write = allow_write and cfg.planner.mode == "apply" and not run.needs_review
        kwargs = {"client_factory": client_factory} if client_factory else {}
        result = publish_service.publish_plan(ctx, run, cfg, now, write=write, **kwargs)
        if write:
            why = "written"
        elif cfg.planner.mode != "apply":
            why = "diff only (planner.mode propose)"
        elif not allow_write:
            why = "diff only (--no-write)"
        else:
            why = "diff only (plan needs review)"
        tail = f"; written {result.written}, deleted {result.deleted}" if write else ""
        if result.needs_review:
            # Written, but read-back found missing events or loads off target: say so loudly.
            tail += f"; CHECK CALENDAR: {len(result.needs_review)} event(s) need review"
        return f"{why}: {result.diff.summary()}{tail}"

    try:
        with job_run(JOB, ctx.factory, log_path=str(ctx.settings.logs_dir / "cyp.jsonl")) as rc:
            stage("sync", "sync.intervals", do_sync)
            stage("analyze", "analysis.rides", do_analyze)
            stage("report.daily", "report.daily", do_daily)
            stage("report.weekly", "report.weekly", do_weekly)
            stage("plan", "plan.horizon", do_plan)
            stage("publish", "publish.calendar", do_publish)
            for s in report.stages:
                rc.incr(s.status)
            if not report.ok:
                failed = [s.name for s in report.stages if s.status == "failed"]
                raise _StagesFailedError(f"failed stages: {', '.join(failed)}")
    except _StagesFailedError:
        pass
    log.info("autopilot.done", profile=report.profile, ok=report.ok)
    save_report(ctx, report, now)
    return report


def record_failure(ctx: AppContext, report: AutopilotReport, now: dt.datetime) -> None:
    """Leave evidence of a run that failed before its stages.

    A failed ``job_runs`` row (best effort: the DB may be the problem) and the saved report.
    """
    failed = ", ".join(f"{st.name}: {st.detail}" for st in report.stages if st.status == "failed")
    try:
        with contextlib.suppress(_StagesFailedError), job_run(JOB, ctx.factory) as rc:
            rc.incr("failed")
            raise _StagesFailedError(failed or "failed")
    except Exception as exc:  # noqa: BLE001 - evidence is best effort
        log.warning("autopilot.job_run_not_recorded", error=str(exc))
    save_report(ctx, report, now)


def runs_dir(ctx: AppContext) -> Path:
    """``data/reports/autopilot``: one JSON report per run (UI history, diagnosis)."""
    return ctx.reports_dir / "autopilot"


def save_report(ctx: AppContext, report: AutopilotReport, now: dt.datetime) -> None:
    """Write ``<runs_dir>/<YYYY-MM-DDTHHMMSS>.json`` (best effort: never fails the run)."""
    try:
        target = runs_dir(ctx)
        target.mkdir(parents=True, exist_ok=True)
        payload = {**report.to_dict(), "at": now.isoformat(timespec="seconds")}
        name = now.strftime("%Y-%m-%dT%H%M%S") + ".json"
        (target / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("autopilot.report_not_saved", error=str(exc))


def recent_runs(ctx: AppContext, limit: int = 20) -> list[dict[str, Any]]:
    """The newest saved run reports, newest first."""
    files = sorted(runs_dir(ctx).glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


class _Skip(Exception):
    """Raised by a stage body to report ``skipped`` with a reason."""


def _brief(counts: dict[str, Any]) -> str:
    """Compact ``k=v`` text of the top-level scalar counts."""
    items = [f"{k}={v}" for k, v in sorted(counts.items()) if isinstance(v, int | float | str)]
    return ", ".join(items) or "done"
