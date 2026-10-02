"""Report commands: ``report daily|weekly`` and the unattended ``daily`` / ``weekly`` pipelines."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from cyp.cli.analysis import echo_readiness
from cyp.cli.common import (
    DEFAULT_ATHLETE_CONFIG,
    SERVICE_ERRORS,
    AthleteConfigOpt,
    DateOpt,
    NoSyncOpt,
    NoWaitOpt,
    app_context,
    cli_settings,
    fmt_counts,
    parse_day,
    today,
)
from cyp.cli.sync import run_unified
from cyp.schemas import ReadinessOut
from cyp.services import pipeline
from cyp.services import reports as reports_service
from cyp.services.context import AppContext

report_app = typer.Typer(help="zh-TW Markdown reports (daily / weekly).", no_args_is_help=True)


def _report_path(ctx: AppContext, kind: str, stem: str) -> Path:
    return Path(ctx.reports_dir) / kind / f"{stem}.md"


def _build_report(ctx: AppContext, kind: reports_service.Kind, day: dt.date) -> None:
    try:
        out = reports_service.build(ctx, kind, day)
    except SERVICE_ERRORS as exc:
        typer.echo(f"report failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{kind} report: {_report_path(ctx, kind, out.stem)}")


@report_app.command("daily")
def report_daily(
    date: DateOpt = None, athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG
) -> None:
    """Render the daily report for --date (default today) from stored results."""
    settings = cli_settings()
    day = parse_day(settings, date)
    with app_context(settings, athlete_config) as ctx:
        _build_report(ctx, "daily", day)


@report_app.command("weekly")
def report_weekly(
    date: DateOpt = None, athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG
) -> None:
    """Render the weekly review of the ISO week containing --date (default: yesterday's week)."""
    settings = cli_settings()
    day = parse_day(settings, date) if date else today(settings) - dt.timedelta(days=1)
    with app_context(settings, athlete_config) as ctx:
        _build_report(ctx, "weekly", day)


def _run_pipeline(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        result: dict[str, Any] = fn()
    except SERVICE_ERRORS as exc:
        typer.echo(f"{name} failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return result


def daily(
    no_sync: NoSyncOpt = False,
    no_wait: NoWaitOpt = False,
    athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG,
) -> None:
    """Unattended daily pipeline: sync, analyze, trends, readiness, report."""
    settings = cli_settings()
    if not no_sync:
        if settings.intervals_api_key.get_secret_value():
            run_unified(no_streams=False, no_wait=no_wait)
        else:
            typer.echo("INTERVALS_API_KEY not set: skipping sync", err=True)
    with app_context(settings, athlete_config) as ctx:
        day = ctx.today()
        out = _run_pipeline("daily", lambda: pipeline.daily(ctx, day=day, sync=False))
        typer.echo(f"analyze: {fmt_counts(out['analyze'])}")
        limiters = out.get("trends", {}).get("limiters", [])
        typer.echo(f"trends: limiters {', '.join(limiters) or 'none'}")
        echo_readiness([ReadinessOut.model_validate(out["readiness"])])
        typer.echo(f"daily report: {_report_path(ctx, 'daily', out['report'])}")


def weekly(
    no_sync: NoSyncOpt = False, athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG
) -> None:
    """Weekly review: trends (FTP/eFTP, durability, TID, limiters) and the weekly report."""
    settings = cli_settings()
    if not no_sync and settings.intervals_api_key.get_secret_value():
        run_unified(no_streams=False, no_wait=True)
    with app_context(settings, athlete_config) as ctx:
        out = _run_pipeline("weekly", lambda: pipeline.weekly(ctx))
        typer.echo(f"weekly report: {_report_path(ctx, 'weekly', out['report'])}")


def register(app: typer.Typer) -> None:
    """Attach ``report``, ``daily`` and ``weekly`` to ``app``."""
    app.add_typer(report_app, name="report")
    app.command()(daily)
    app.command()(weekly)
