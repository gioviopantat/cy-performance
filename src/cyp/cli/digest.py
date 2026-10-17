"""``digest weekly|monthly`` (build or send one digest) and ``digest run [--all]`` (07:00 job)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

import typer

from cyp.cli.common import app_context, cli_settings, fail
from cyp.core.errors import CypError
from cyp.services.digest import Kind
from cyp.settings import Settings

digest_app = typer.Typer(
    help="Weekly / monthly digest PDFs (docs/specs/digest-reports.md).", no_args_is_help=True
)

SendOpt = Annotated[
    bool,
    typer.Option("--send", help="Also email it (needs notify.email) and record it; resends."),
]


@digest_app.command("weekly")
def weekly(
    period: Annotated[
        str | None,
        typer.Option("--period", help="Monday of the ISO week, YYYY-MM-DD (default: last week)."),
    ] = None,
    send: SendOpt = False,
) -> None:
    """Build one week's zh-TW digest PDF under data/reports/digest (needs report.digest_weekly)."""
    start = _parse(period, "%Y-%m-%d", "YYYY-MM-DD") if period else None
    _one("weekly", start, send)


@digest_app.command("monthly")
def monthly(
    period: Annotated[
        str | None,
        typer.Option("--period", help="Month, YYYY-MM (default: last month)."),
    ] = None,
    send: SendOpt = False,
) -> None:
    """Build one month's zh-TW digest PDF in data/reports/digest (needs report.digest_monthly)."""
    start = _parse(period, "%Y-%m", "YYYY-MM") if period else None
    _one("monthly", start, send)


def _parse(value: str, fmt: str, hint: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, fmt).date()
    except ValueError:
        fail(f"--period {value!r}: expected {hint}", code=2)


def _one(kind: Kind, start: dt.date | None, send: bool) -> None:
    from cyp.jobs.digest import latest_due
    from cyp.services import digest as service

    with app_context(cli_settings()) as ctx:
        period = start or latest_due(kind, dt.datetime.now(dt.UTC), ctx.tz)
        try:
            if not send:
                typer.echo(str(service.build(ctx, kind, period)))
                return
            if not ctx.features().enabled("notify.email"):
                fail("feature notify.email is off for this profile", code=2)
            out = service.send(ctx, kind, period)
        except CypError as exc:
            fail(str(exc), code=1)
    typer.echo(f"{out.status} (attempt {out.attempts}): {out.pdf_path or '-'}")
    if not out.ok:
        fail(out.error or out.status, code=1)


@digest_app.command("run")
def run(
    all_profiles: Annotated[
        bool, typer.Option("--all", help="Every profile, one subprocess each.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="One JSON line per profile.")] = False,
) -> None:
    """Build and email every due, unsent digest once (the 07:00 launch agent runs this).

    Does nothing for profiles with both digest flags off. Never writes a calendar or Strava.
    Exit 1 when a run or a digest failed.
    """
    from cyp.jobs.digest import digest_all, digest_profile
    from cyp.profiles import open_store

    if all_profiles:
        slugs = [p.slug for p in open_store(Settings().cyp_profiles_dir).list()]
        if not slugs:
            fail("no profiles: `cyp profile add <name>` or `cyp profile adopt <name>`", code=2)
        results = digest_all(slugs)
        for r in results:
            payload = r.report or {"profile": r.profile, "ok": False, "error": r.stderr_tail}
            if as_json:
                typer.echo(json.dumps(payload, ensure_ascii=False))
            else:
                _echo(payload)
        if not all(r.ok for r in results):
            raise typer.Exit(code=1)
        return
    report = digest_profile(cli_settings())
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
    for d in report["digests"]:
        typer.echo(
            f"  {d['action']:<8s} {d['kind']:<8s} {d['period_start']} {d['detail']}".rstrip()
        )


def register(app: typer.Typer) -> None:
    """Attach ``digest`` to ``app``."""
    app.add_typer(digest_app, name="digest")
