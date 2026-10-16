"""``watch [--all]`` (the ride watcher tick) and ``ride-report`` (build / resend one ride's PDF)."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from cyp.cli.common import app_context, cli_settings, fail
from cyp.cli.ridelog import ActivityArg, _resolve
from cyp.core.errors import CypError
from cyp.settings import Settings


def watch(
    all_profiles: Annotated[
        bool, typer.Option("--all", help="Every profile, one subprocess each.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON line per profile.")] = False,
) -> None:
    """One ride-watcher tick: sync, then email the PDF report of each new ride once.

    Does nothing for profiles with ``report.ride`` off. Never writes a calendar or Strava.
    Exit 1 when a tick or a report failed.
    """
    from cyp.jobs.watch import watch_all, watch_profile
    from cyp.profiles import open_store

    if all_profiles:
        slugs = [p.slug for p in open_store(Settings().cyp_profiles_dir).list()]
        if not slugs:
            fail("no profiles: `cyp profile add <name>` or `cyp profile adopt <name>`", code=2)
        results = watch_all(slugs)
        for r in results:
            payload = r.report or {"profile": r.profile, "ok": False, "error": r.stderr_tail}
            if as_json:
                typer.echo(json.dumps(payload, ensure_ascii=False))
            else:
                _echo(payload)
        if not all(r.ok for r in results):
            raise typer.Exit(code=1)
        return
    report = watch_profile(cli_settings())
    if as_json:
        typer.echo(json.dumps(report.to_dict(), ensure_ascii=False))
    else:
        _echo(report.to_dict())
    if not report.ok:
        raise typer.Exit(code=1)


def _echo(report: dict[str, Any]) -> None:
    if "status" not in report:
        typer.echo(f"[{report['profile']}] FAILED: {str(report.get('error', '')).strip()}")
        return
    detail = f": {report['detail']}" if report.get("detail") else ""
    typer.echo(f"[{report['profile']}] {report['status']}{detail}")
    for r in report["rides"]:
        typer.echo(f"  {r['action']:<13s} {r['activity_id']} {r['detail']}".rstrip())


def ride_report(
    activity: ActivityArg = "latest",
    send: Annotated[
        bool, typer.Option("--send", help="Also email it (needs notify.email); resends.")
    ] = False,
) -> None:
    """Build one ride's zh-TW PDF report under data/reports/ride (needs report.ride)."""
    from cyp.services import ride_report as service

    with app_context(cli_settings()) as ctx:
        activity_id = _resolve(ctx, activity)
        try:
            if not send:
                typer.echo(str(service.build(ctx, activity_id)))
                return
            if not ctx.features().enabled("notify.email"):
                fail("feature notify.email is off for this profile", code=2)
            out = service.send(ctx, activity_id)
        except CypError as exc:
            fail(str(exc), code=1)
    typer.echo(f"{out.status} (attempt {out.attempts}): {out.pdf_path or '-'}")
    if not out.ok:
        fail(out.error or out.status, code=1)


def register(app: typer.Typer) -> None:
    """Attach ``watch`` and ``ride-report`` to ``app``."""
    app.command()(watch)
    app.command("ride-report")(ride_report)
