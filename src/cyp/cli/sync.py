"""Data-source commands: ``sync`` (+ subcommands), ``backfill``, ``auth strava``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from cyp.cli.common import NoStreamsOpt, NoWaitOpt, cli_settings, fail, fmt_counts, log
from cyp.core.errors import CypError
from cyp.ingest.matcher import JOB as MATCH_JOB
from cyp.ingest.matcher import Matcher
from cyp.ingest.strava.client import StravaClient
from cyp.ingest.strava.oauth import StravaAuth, TokenStore, import_token_file
from cyp.ingest.strava.sync import StravaSyncer
from cyp.jobs.sync import JOB_STRAVA, SyncResult, run_sync
from cyp.settings import Settings
from cyp.store.db import engine_from_settings, session_factory
from cyp.store.migrate import schema_status
from cyp.store.runs import job_run
from cyp.store.streams import StreamStore

auth_app = typer.Typer(help="OAuth / API-key setup for the data sources.", no_args_is_help=True)
sync_app = typer.Typer(
    help="Pull data from the sources. Without a subcommand runs every enabled source.",
    invoke_without_command=True,
)


@sync_app.callback()
def sync(ctx: typer.Context, no_streams: NoStreamsOpt = False, no_wait: NoWaitOpt = False) -> None:
    """Unified incremental sync: intervals.icu -> matcher -> Strava -> matcher."""
    if ctx.invoked_subcommand is not None:
        return
    run_unified(no_streams=no_streams, no_wait=no_wait)


def backfill(
    days: Annotated[
        int, typer.Option("--days", min=1, help="Days of history to import (paged by month).")
    ],
    no_streams: NoStreamsOpt = False,
    no_wait: NoWaitOpt = False,
) -> None:
    """History import: intervals.icu backfill -> matcher -> Strava -> matcher (resumable)."""
    run_unified(backfill_days=days, no_streams=no_streams, no_wait=no_wait)


def run_unified(*, backfill_days: int | None = None, no_streams: bool, no_wait: bool) -> None:
    """Schema guard, then the unified sync; prints per-stage counts.

    Exits 2 when the schema is not at head or Strava rate-limited the run, 1 on sync errors.
    """
    settings = cli_settings()
    engine = engine_from_settings(settings)
    try:
        status = schema_status(engine, settings.cyp_db_url)
    finally:
        engine.dispose()
    if not status.up_to_date:
        fail("database schema is not at head (run `cyp init` and `cyp db upgrade`)", code=2)
    try:
        result = run_sync(
            settings, backfill_days=backfill_days, streams=not no_streams, no_wait=no_wait
        )
    except CypError as exc:
        typer.echo(f"sync failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_sync_result(result)
    if result.strava_rate_limited:
        typer.echo("stopped: Strava rate limited (cursor persisted; re-run after the window)")
        raise typer.Exit(code=2)


def _echo_sync_result(result: SyncResult) -> None:
    typer.echo("intervals.icu")
    for name, counts in result.intervals.items():
        typer.echo(f"  {name}: {fmt_counts(counts)}")
    typer.echo(f"match (after intervals): {fmt_counts(result.match_before_strava)}")
    typer.echo("strava")
    if result.strava is None:
        typer.echo(f"  skipped ({result.strava_skipped_reason or 'not run'})")
    else:
        typer.echo(f"  {fmt_counts(result.strava)}")
        if result.strava_remaining_15m is not None:
            typer.echo(f"  remaining_15m: {result.strava_remaining_15m}")
    if result.match_after_strava is not None:
        typer.echo(f"match (after strava): {fmt_counts(result.match_after_strava)}")
    for err in result.errors:
        typer.echo(f"  error: {err}")
    log.info("sync.unified.done", job=result.job, stages=result.stages_run, errors=result.errors)


# ------------------------------------------------------------------------------------ strava


def _strava_auth(settings: Settings) -> StravaAuth:
    return StravaAuth(settings, TokenStore.from_settings(settings))


@auth_app.command("strava")
def auth_strava(
    import_from: Annotated[
        Path | None,
        typer.Option(
            "--import-from",
            help="Copy an existing strava-analyis token.json instead of running the OAuth flow.",
        ),
    ] = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the URL only; do not open a browser.")
    ] = False,
) -> None:
    """Authorize with Strava via the local callback (port 8721) or import a token file."""
    import cyp.cli as cli_pkg  # resolved at call time so tests can stub the interactive flow

    settings = cli_settings()
    store = TokenStore.from_settings(settings)
    try:
        if import_from is not None:
            token = import_token_file(import_from, store)
        else:
            token = cli_pkg.run_local_callback_flow(
                _strava_auth(settings), open_browser=not no_browser, echo=typer.echo
            )
    except CypError as exc:
        typer.echo(f"auth strava failed: {exc}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"token saved to {store.path} (mode 600)")
    typer.echo(f"athlete_id={token.athlete_id} scope={token.scope} expires_at={token.expires_at}")


@sync_app.command("strava")
def sync_strava(
    full: Annotated[
        bool, typer.Option("--full", help="Ignore the cursor; re-list history.")
    ] = False,
    streams: Annotated[
        bool,
        typer.Option("--streams", help="Fetch Strava streams for rides that have no Parquet yet."),
    ] = False,
    no_wait: Annotated[
        bool,
        typer.Option(
            "--no-wait", help="On rate limit: persist the cursor and exit instead of sleeping."
        ),
    ] = False,
    max_detail: Annotated[
        int | None,
        typer.Option(
            "--max-detail",
            min=0,
            help="Max detail + fallback-stream fetches this run "
            "(default: STRAVA_MAX_DETAIL_FETCHES).",
        ),
    ] = None,
) -> None:
    """Strava only: summaries, detail + segment efforts, zones (+ fallback streams), matcher."""
    settings = cli_settings()
    if not settings.strava_enabled:
        typer.echo("strava sync skipped (feature sync.strava is off)")
        return
    factory = session_factory(engine_from_settings(settings))
    auth = _strava_auth(settings)
    client = StravaClient(
        auth.get_valid_access_token,
        auth.force_refresh_access_token,
        wait_on_rate_limit=not no_wait,
    )
    syncer = StravaSyncer(
        settings,
        factory,
        client,
        max_detail_fetches=(
            max_detail if max_detail is not None else settings.strava_max_detail_fetches
        ),
        fetch_streams=streams,
    )
    with job_run(JOB_STRAVA, factory) as ctx:
        summary = syncer.run(ctx, full=full)
    with job_run(MATCH_JOB, factory) as mctx:
        match = Matcher(factory, StreamStore(settings.streams_dir)).rematch_all(mctx)
    for key, value in sorted(summary.counts().items()):
        typer.echo(f"  {key}: {value}")
    typer.echo(f"  match: {fmt_counts(match.counts())}")
    typer.echo(
        f"  cursor: {summary.cursor_before} -> {summary.cursor_after}"
        f"  remaining_15m: {summary.rate_limit.get('remaining_15m')}"
    )
    for err in summary.errors:
        typer.echo(f"  error: {err}")
    if summary.rate_limited:
        typer.echo("stopped: rate limited (cursor persisted; re-run after the window resets)")
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------------- intervals


def _intervals_key(settings: Settings) -> str:
    key = settings.intervals_api_key.get_secret_value()
    if not key:
        fail("INTERVALS_API_KEY is not set (see .env.example)", code=2)
    return key


@sync_app.command("intervals")
def sync_intervals(
    backfill_days: Annotated[
        int | None,
        typer.Option(
            "--backfill-days",
            min=1,
            help="Import this many days of history (paged by month, resumable) instead of an "
            "incremental sync.",
        ),
    ] = None,
    no_streams: NoStreamsOpt = False,
    stage: Annotated[
        list[str] | None,
        typer.Option(
            "--stage",
            help="Run only these stages (athlete, activities, wellness, power_curves, events).",
        ),
    ] = None,
) -> None:
    """intervals.icu only: athlete, activities (+streams, intervals), wellness, curves, events."""
    from cyp.ingest.intervals.auth import ApiKeyAuth
    from cyp.ingest.intervals.client import IntervalsClient
    from cyp.ingest.intervals.sync import STAGES, IntervalsSyncer, SyncOptions

    settings = cli_settings()
    key = _intervals_key(settings)
    stages = tuple(stage) if stage else STAGES
    unknown = [st for st in stages if st not in STAGES]
    if unknown:
        fail(f"unknown stage(s): {', '.join(unknown)}; expected {', '.join(STAGES)}", code=2)
    engine = engine_from_settings(settings)
    factory = session_factory(engine)
    client = IntervalsClient(auth=ApiKeyAuth(key), athlete_id=settings.intervals_athlete_id)
    syncer = IntervalsSyncer(
        client,
        factory,
        StreamStore(settings.streams_dir),
        options=SyncOptions(fetch_streams=not no_streams),
        timezone=settings.cyp_timezone,
        log_path=str(settings.logs_dir / "cyp.jsonl"),
    )
    try:
        if backfill_days is not None:
            report = syncer.backfill(backfill_days, stages=stages)
        else:
            report = syncer.run(stages)
    except CypError as exc:
        typer.echo(f"sync intervals failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
    try:
        with job_run(MATCH_JOB, factory, log_path=str(settings.logs_dir / "cyp.jsonl")) as mctx:
            match = Matcher(factory, StreamStore(settings.streams_dir)).rematch_all(mctx)
    finally:
        engine.dispose()
    for name, counts in report.stages.items():
        typer.echo(f"  {name}: {fmt_counts(counts)}")
    typer.echo(f"  match: {fmt_counts(match.counts())}")
    rl = client.rate_limit
    if rl.limit is not None or rl.remaining is not None:
        typer.echo(f"  rate limit: {rl.remaining}/{rl.limit} remaining")
    log.info(
        "sync.intervals.done", stages=list(report.stages), athlete_id=client.resolved_athlete_id
    )


@sync_app.command("refetch-streams")
def sync_refetch_streams(
    activity: Annotated[
        list[int] | None,
        typer.Option("--activity", help="Only these internal activity ids (repeatable)."),
    ] = None,
) -> None:
    """Re-download icu streams and rewrite Parquet (e.g. after the lat/lng mapping fix).

    Every rewritten ride is marked pending_analysis; run `cyp analyze` afterwards so climb
    fingerprints are recomputed.
    """
    from cyp.ingest.intervals.auth import ApiKeyAuth
    from cyp.ingest.intervals.client import IntervalsClient
    from cyp.ingest.intervals.sync import IntervalsSyncer

    settings = cli_settings()
    key = _intervals_key(settings)
    engine = engine_from_settings(settings)
    client = IntervalsClient(auth=ApiKeyAuth(key), athlete_id=settings.intervals_athlete_id)
    syncer = IntervalsSyncer(
        client,
        session_factory(engine),
        StreamStore(settings.streams_dir),
        timezone=settings.cyp_timezone,
        log_path=str(settings.logs_dir / "cyp.jsonl"),
    )
    try:
        counts = syncer.refetch_streams(activity)
    except CypError as exc:
        typer.echo(f"refetch failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        client.close()
        engine.dispose()
    typer.echo(f"refetch-streams: {fmt_counts(counts)}")
    typer.echo("next: `cyp analyze` to recompute climbs/fingerprints from the new files")


def register(app: typer.Typer) -> None:
    """Attach ``auth``, ``sync`` and ``backfill`` to ``app``."""
    app.add_typer(auth_app, name="auth")
    app.add_typer(sync_app, name="sync")
    app.command()(backfill)
