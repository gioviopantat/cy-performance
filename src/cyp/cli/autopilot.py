"""Autopilot commands (ADR-0006 L2/L3): ``run``, ``schedule install|uninstall|status``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from cyp.cli.common import cli_settings, fail
from cyp.core.errors import CypError
from cyp.settings import Settings

schedule_app = typer.Typer(help="Daily autopilot schedule (macOS launchd).", no_args_is_help=True)


def run(
    all_profiles: Annotated[
        bool, typer.Option("--all", help="Every profile, one subprocess each.")
    ] = False,
    no_write: Annotated[
        bool, typer.Option("--no-write", help="Diff only, even for profiles in apply mode.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON line per profile.")] = False,
) -> None:
    """Sync, analyse, report, plan and publish (each stage behind a feature flag).

    Writes to the calendar only for profiles whose athlete.yaml says ``planner.mode: apply``
    (set with ``cyp profile promote``). Exit 1 when any stage of any profile failed.
    """
    from cyp.jobs.runner import run_all, run_profile
    from cyp.profiles import open_store

    if all_profiles:
        profiles_dir = Settings().cyp_profiles_dir
        slugs = [p.slug for p in open_store(profiles_dir).list()]
        if not slugs:
            fail("no profiles: `cyp profile add <name>` or `cyp profile adopt <name>`", code=2)
        results = run_all(profiles_dir, slugs, no_write=no_write)
        for r in results:
            if as_json:
                payload = r.report or {"profile": r.profile, "ok": False, "error": r.stderr_tail}
                typer.echo(json.dumps(payload, ensure_ascii=False))
            elif r.report:
                _echo_report(r.report)
            else:
                typer.echo(f"[{r.profile}] FAILED (exit {r.exit_code}): {r.stderr_tail.strip()}")
        if not all(r.ok for r in results):
            raise typer.Exit(code=1)
        return
    report = run_profile(cli_settings(), no_write=no_write)
    if as_json:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        _echo_report(report.to_dict())
    if not report.ok:
        raise typer.Exit(code=1)


def _echo_report(report: dict[str, Any]) -> None:
    status = "ok" if report.get("ok") else "FAILED"
    typer.echo(f"[{report['profile']}] {report['day']} mode={report['planner_mode']} -> {status}")
    for s in report["stages"]:
        typer.echo(f"  {s['status']:<7s} {s['name']:<14s} {s['detail']}")


# ---------------------------------------------------------------------------------- schedule


@schedule_app.command("install")
def schedule_install(
    at: Annotated[str, typer.Option("--at", help="Local time HH:MM.")] = "05:30",
    watch: Annotated[
        bool,
        typer.Option("--watch", help="Install the ride watcher (`cyp watch --all` every 10 min)."),
    ] = False,
) -> None:
    """Install/refresh the launchd agent running ``cyp run --all`` daily (catches up on wake).

    ``--watch`` installs the second agent instead; the autopilot agent is left as it is.
    """
    from cyp.jobs.launchd import AgentSpec, install, watch_spec

    workdir = Path.cwd().resolve()
    if watch:
        spec = watch_spec(workdir)
        try:
            path = install(spec)
        except CypError as exc:
            fail(str(exc), code=1)
        typer.echo(f"installed {path}: every {spec.interval_s}s from {spec.workdir}")
        typer.echo(f"log: {spec.log_path}")
        return
    try:
        hour, minute = (int(x) for x in at.split(":"))
        spec = AgentSpec(workdir=workdir, hour=hour, minute=minute)
        path = install(spec)
    except ValueError:
        fail(f"--at {at!r}: expected HH:MM", code=2)
    except CypError as exc:
        fail(str(exc), code=1)
    typer.echo(f"installed {path}: daily at {hour:02d}:{minute:02d} from {spec.workdir}")
    typer.echo(f"log: {spec.log_path}")


@schedule_app.command("uninstall")
def schedule_uninstall(
    watch: Annotated[bool, typer.Option("--watch", help="Remove the ride watcher agent.")] = False,
) -> None:
    """Remove the launchd agent (``--watch``: the ride watcher's)."""
    from cyp.jobs.launchd import LABEL, WATCH_LABEL, uninstall

    typer.echo("removed" if uninstall(WATCH_LABEL if watch else LABEL) else "no agent installed")


@schedule_app.command("status")
def schedule_status() -> None:
    """Whether each agent (autopilot, ride watcher) is loaded, its schedule and last exit status.

    Exit 1 when the autopilot agent is not loaded (the watcher is optional).
    """
    from cyp.jobs.launchd import LABEL, WATCH_LABEL, status

    keep = (
        "state =",
        "last exit code",
        "program",
        "Hour",
        "Minute",
        "StartInterval",
        "run interval",
        "working directory",
    )
    autopilot_loaded = True
    for label, hint in ((LABEL, "`cyp schedule install`"), (WATCH_LABEL, "`--watch`")):
        out = status(label)
        if out is None:
            typer.echo(f"{label}: not loaded ({hint})")
            autopilot_loaded = autopilot_loaded and label != LABEL
            continue
        typer.echo(f"{label}:")
        for line in out.splitlines():
            if any(k in line for k in keep):
                typer.echo(line.rstrip())
    if not autopilot_loaded:
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    """Attach ``run`` and ``schedule`` to ``app``."""
    app.command()(run)
    app.add_typer(schedule_app, name="schedule")
