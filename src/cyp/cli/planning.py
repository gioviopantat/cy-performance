"""Planning commands: ``plan``, ``season``, ``publish spike``."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

import typer

from cyp.analysis.longitudinal.run import load_latest_report
from cyp.cli.common import (
    DEFAULT_ATHLETE_CONFIG,
    SERVICE_ERRORS,
    AthleteConfigOpt,
    DateOpt,
    JsonOpt,
    app_context,
    cli_settings,
    echo_json,
    fail,
    parse_day,
    require_athlete_config,
    today,
)
from cyp.core.errors import CypError
from cyp.planning.job import PlanRun, build_plan
from cyp.publish.plan_events import climbs_from_config, event_specs
from cyp.schemas import PlanOut, PlanPreviewRequest, SeasonOut
from cyp.services import plan as plan_service
from cyp.services.context import AppContext
from cyp.settings import AthleteConfig, Settings
from cyp.store.repo.sync_cursors import SyncCursorRepo

publish_app = typer.Typer(help="intervals.icu calendar publishing (M3).", no_args_is_help=True)
PUBLISH_CURSOR_SOURCE = "intervals"
PUBLISH_CURSOR_MODE = "publish_upsert_mode"


def plan(
    date: DateOpt = None,
    publish: Annotated[
        bool,
        typer.Option("--publish", help="Compare with the live icu calendar (read-only diff)."),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Write the diff to intervals.icu (needs --confirm-write)."),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option("--confirm-write", help="Required with --apply: really change your calendar."),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Do not store the plan (print only).")
    ] = False,
    as_json: JsonOpt = False,
    athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG,
) -> None:
    """Replan the rolling horizon (propose by default) and show/apply the calendar diff."""
    settings = cli_settings()
    cfg = require_athlete_config(athlete_config)
    if apply and not confirm:
        fail("--apply writes to your intervals.icu calendar; add --confirm-write", code=2)
    day = parse_day(settings, date)
    # An explicit past/future --date plans as of 06:00 that day; otherwise "now".
    as_of = day if date is not None and day != today(settings) else None
    with app_context(settings, athlete_config) as ctx:
        now_local = ctx.now_local() if as_of is None else dt.datetime.combine(day, dt.time(6, 0))
        run: PlanRun | None = None
        try:
            if publish or apply:
                # The publisher needs the PlanRun's event specs, so build it directly here.
                bias = (load_latest_report(ctx.reports_dir) or {}).get("planner_bias", {})
                run = build_plan(
                    ctx.factory, cfg, today=day, now_local=now_local, bias=bias, persist=not dry_run
                )
                out = plan_service.to_out(ctx.dataset(), run, persisted=not dry_run)
            elif dry_run:
                out = plan_service.preview(ctx, PlanPreviewRequest(today=as_of))
            else:
                out = plan_service.commit(ctx, today=as_of)
        except SERVICE_ERRORS as exc:
            typer.echo(f"plan failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    if as_json:
        echo_json(out)
    else:
        echo_plan(out)
    if run is not None:
        _publish_plan(settings, run, now_local, apply=apply, cfg=cfg)
    if out.needs_review:
        raise typer.Exit(code=3)


def echo_plan(out: PlanOut) -> None:
    """Text rendering of a plan: week headlines, one line per day, adaptations, guards."""
    typer.echo(f"plan {out.today} (season {out.season_key}, mode propose)")
    for w in out.weeks:
        if w.explanation is not None:
            typer.echo(f"  {w.explanation.headline_zh}")
    for d in out.days:
        if d.name_zh is None:
            label = "休息"
        else:
            venue = "室外" if d.outdoor else "室內"
            label = f"{d.name_zh}  {d.tss:.0f} TSS  {d.minutes} 分  {venue}"
        typer.echo(f"  {d.date} {d.date.strftime('%a')}  {label}")
    for e in out.adaptations:
        typer.echo(f"  調整：{e.headline_zh}")
    for r in out.repairs:
        typer.echo(f"  守衛：{r.headline_zh}")
    for v in out.violations:
        typer.echo(f"  NEEDS REVIEW {v['rule']} {v['date']}: {v['detail_zh']}")
    if out.changes:
        typer.echo("  changes: " + ", ".join(f"{k} {len(v)}" for k, v in out.changes.items()))


def _publish_plan(
    settings: Settings,
    run: PlanRun,
    now_local: dt.datetime,
    *,
    apply: bool,
    cfg: AthleteConfig | None = None,
) -> None:
    """Diff (and with ``apply`` write) the plan against the live intervals.icu calendar."""
    from cyp.ingest.intervals.auth import ApiKeyAuth
    from cyp.ingest.intervals.client import IntervalsClient
    from cyp.publish.publisher import Publisher

    key = settings.intervals_api_key.get_secret_value()
    if not key:
        fail("INTERVALS_API_KEY is not set: cannot compare with the calendar", code=2)
    with app_context(settings) as ctx:
        with ctx.factory() as s:
            mode = SyncCursorRepo(s).get(PUBLISH_CURSOR_SOURCE, PUBLISH_CURSOR_MODE)
        if apply and mode not in ("upsert", "uid"):
            fail("run `cyp publish spike --confirm-write` first to learn the upsert mode", code=2)
        specs = event_specs(run, climbs=climbs_from_config(cfg))
        window = (run.days[0].date, run.days[-1].date) if run.days else (run.today, run.today)
        client = IntervalsClient(auth=ApiKeyAuth(key), athlete_id=settings.intervals_athlete_id)
        try:
            upsert_mode: Literal["upsert", "uid"] = "uid" if mode == "uid" else "upsert"
            result = Publisher(client, ctx.factory, upsert_mode=upsert_mode).run(
                specs,
                window=window,
                now_local=now_local,
                mode="apply" if apply else "propose",
                allow_write=apply,
            )
        except CypError as exc:
            typer.echo(f"publish failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        finally:
            client.close()
    typer.echo(f"calendar diff: {result.diff.summary()}")
    if apply:
        typer.echo(
            f"written {result.written}, deleted {result.deleted}, "
            f"verified {len(result.verified)}, needs_review {len(result.needs_review)}"
        )


# ------------------------------------------------------------------------------------ season


def echo_season(out: SeasonOut) -> None:
    """Header plus one row per season week."""
    goal = f"{out.goal_name} " if out.goal_name else ""
    target = f"(target FTP {out.target_ftp:.0f} W) " if out.target_ftp else ""
    typer.echo(
        f"season {out.start} -> {out.goal_date}  {goal}{target}CTL start {out.ctl_start:.1f}"
    )
    typer.echo(
        "   wk  start       phase   flags           HIT   TSS  hours  CTL end  FTP ckpt  階段"
    )
    for w in out.weeks:
        flags = ",".join(f for f in ("recovery" if w.recovery else "", w.test or "") if f) or "-"
        ckpt = f"{w.checkpoint_ftp:.0f}" if w.checkpoint_ftp else ""
        typer.echo(
            f"  {w.index:3d}  {w.start}  {w.phase:<7s} {flags:<15s} {w.hit_sessions:3d}  "
            f"{w.target_tss:4.0f}  {w.target_hours:5.1f}  {w.ctl_end:7.1f}  {ckpt:>8s}  "
            f"{w.phase_zh}"
        )


def season(
    as_json: JsonOpt = False,
    athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG,
) -> None:
    """Whole-season overview: phase, weekly TSS / hours targets and projected CTL per week."""
    settings = cli_settings()
    require_athlete_config(athlete_config)
    with app_context(settings, athlete_config) as ctx:
        out = _season(ctx)
    if as_json:
        echo_json(out)
    else:
        echo_season(out)


def _season(ctx: AppContext) -> SeasonOut:
    try:
        return plan_service.season(ctx)
    except SERVICE_ERRORS as exc:
        typer.echo(f"season failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


# ----------------------------------------------------------------------------------- publish


@publish_app.command("spike")
def publish_spike(
    date: Annotated[
        str,
        typer.Option("--date", help="Future local date for the throw-away event (YYYY-MM-DD)."),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm-write",
            help="Actually write (and then delete) the test event on your intervals.icu calendar.",
        ),
    ] = False,
) -> None:
    """Find out whether events/bulk upsert works with this API key (writes only with confirm).

    Without --confirm-write it only prints what it would do. With it, it creates one test
    WORKOUT on --date, re-posts it to test upsert, counts copies, deletes everything it made
    and stores the supported mode for the publisher.
    """
    from cyp.ingest.intervals.auth import ApiKeyAuth
    from cyp.ingest.intervals.client import IntervalsClient
    from cyp.publish.spike import plan_spike, run_spike

    settings = cli_settings()
    day = parse_day(settings, date)
    if day <= today(settings):
        fail("--date must be in the future (past/today are never written)", code=2)
    if not confirm:
        typer.echo(f"dry run — would write to your intervals.icu calendar on {day}:")
        for line in plan_spike(day):
            typer.echo(f"  - {line}")
        typer.echo("re-run with --confirm-write to execute (the test event is deleted afterwards)")
        return
    key = settings.intervals_api_key.get_secret_value()
    if not key:
        fail("INTERVALS_API_KEY is not set (see .env.example)", code=2)
    client = IntervalsClient(auth=ApiKeyAuth(key), athlete_id=settings.intervals_athlete_id)
    try:
        result = run_spike(client, day)
    except CypError as exc:
        typer.echo(f"spike failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
    typer.echo(f"copies after two posts: {result.copies}")
    for note in result.notes:
        typer.echo(f"  {note}")
    typer.echo(f"supported upsert mode: {result.supported}")
    typer.echo(f"cleaned up {result.cleaned_up} event(s); leftovers: {result.leftovers or 'none'}")
    if result.supported != "none":
        with app_context(settings) as ctx, ctx.factory() as s:
            SyncCursorRepo(s).set(PUBLISH_CURSOR_SOURCE, PUBLISH_CURSOR_MODE, result.supported)
            s.commit()
    if result.leftovers:
        fail("WARNING: delete the leftover test events manually in intervals.icu", code=1)


def register(app: typer.Typer) -> None:
    """Attach ``plan``, ``season`` and ``publish`` to ``app``."""
    app.command()(plan)
    app.command()(season)
    app.add_typer(publish_app, name="publish")
