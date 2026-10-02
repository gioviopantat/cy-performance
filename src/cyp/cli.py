"""``cyp`` command-line entry point (docs/01 §5.9).

M0: ``init``, ``doctor``, ``db upgrade``, ``explain``. M1: ``auth strava``, ``sync`` (unified),
``sync intervals``, ``sync strava``, ``sync refetch-streams``, ``backfill``. M2: ``analyze``
(ride -> trends -> readiness), ``trends``, ``readiness``. Later milestones' commands are stubs.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import Engine, select

from cyp import __version__
from cyp.core.errors import ConfigError, CypError
from cyp.ingest.intervals.sync import (
    CURSOR_ACTIVITIES as ICU_CURSOR_ACTIVITIES,
)
from cyp.ingest.intervals.sync import (
    CURSOR_ATHLETE_ID as ICU_CURSOR_ATHLETE_ID,
)
from cyp.ingest.intervals.sync import (
    CURSOR_WELLNESS as ICU_CURSOR_WELLNESS,
)
from cyp.ingest.intervals.sync import (
    SOURCE as ICU_SOURCE,
)
from cyp.ingest.matcher import JOB as MATCH_JOB
from cyp.ingest.matcher import Matcher
from cyp.ingest.strava.client import StravaClient
from cyp.ingest.strava.oauth import (
    StravaAuth,
    TokenStore,
    import_token_file,
    run_local_callback_flow,
    token_status,
)
from cyp.ingest.strava.sync import (
    CURSOR_AFTER,
    CURSOR_LAST_SYNC,
    CURSOR_RATE_SNAPSHOT,
    StravaSyncer,
)
from cyp.ingest.strava.sync import (
    SOURCE as STRAVA_SOURCE,
)
from cyp.jobs.runs import job_run
from cyp.jobs.sync import JOB_BACKFILL, JOB_STRAVA, JOB_SYNC, SyncResult, run_sync
from cyp.logging import configure_logging, get_logger
from cyp.settings import DEFAULT_ATHLETE_CONFIG, Settings, get_settings, load_athlete_config
from cyp.store.db import engine_from_settings, ping, session_factory
from cyp.store.migrate import schema_status, upgrade_head
from cyp.store.models import EXPLANATION_TABLES, Activity, StreamFile
from cyp.store.repo.job_runs import JobRunRepo
from cyp.store.repo.sync_cursors import SyncCursorRepo
from cyp.store.streams import StreamStore

app = typer.Typer(
    name="cyp",
    help="Cycling performance analysis + adaptive plans for intervals.icu.",
    no_args_is_help=True,
    rich_markup_mode=None,
)
db_app = typer.Typer(help="Database schema management.", no_args_is_help=True)
app.add_typer(db_app, name="db")
auth_app = typer.Typer(help="OAuth / API-key setup for the data sources.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
sync_app = typer.Typer(
    help="Pull data from the sources. Without a subcommand runs every enabled source.",
    invoke_without_command=True,
)
app.add_typer(sync_app, name="sync")

log = get_logger("cyp.cli")

JOBS = (
    "daily",
    "weekly",
    JOB_SYNC,
    JOB_BACKFILL,
    MATCH_JOB,
    JOB_STRAVA,
    "analyze",
    "plan",
    "publish",
)

AthleteConfigOpt = Annotated[
    Path,
    typer.Option("--athlete-config", help="Path to athlete.yaml", show_default=True),
]


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.logs_dir, console=False)
    return settings


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cyp {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
) -> None:
    """cyp: analyse rides, track fitness, plan training."""


# ------------------------------------------------------------------------------- init / doctor


@app.command()
def init() -> None:
    """Create the data directory layout (streams/, tokens/, logs/, reports/)."""
    settings = get_settings()
    for path in settings.data_layout:
        path.mkdir(parents=True, exist_ok=True)
    settings.tokens_dir.chmod(0o700)
    configure_logging(settings.logs_dir, console=False)
    typer.echo(f"data dir ready: {settings.cyp_data_dir.resolve()}")
    for path in settings.data_layout[1:]:
        typer.echo(f"  {path.relative_to(settings.cyp_data_dir)}/")
    log.info("init.done", data_dir=str(settings.cyp_data_dir))


@app.command()
def doctor(athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG) -> None:
    """Report settings (masked), data dir, DB + schema, athlete config, tokens, last job runs."""
    settings = get_settings()
    problems = 0

    def row(label: str, value: object, ok: bool | None = None, *, warn: bool = False) -> None:
        if ok is None:
            mark = ""
        elif ok:
            mark = "OK   "
        else:
            mark = "WARN " if warn else "FAIL "
        typer.echo(f"  {mark}{label}: {value}")

    typer.echo("Settings")
    for key, value in settings.masked_summary().items():
        row(key, value)

    typer.echo("Data dir")
    for path in settings.data_layout:
        exists = path.is_dir()
        problems += not exists
        row(str(path), "present" if exists else "missing (run `cyp init`)", exists)

    typer.echo("Database")
    engine = engine_from_settings(settings)
    reachable = ping(engine)
    problems += not reachable
    row(settings.cyp_db_url, "reachable" if reachable else "unreachable", reachable)
    if reachable:
        status = schema_status(engine, settings.cyp_db_url)
        problems += not status.up_to_date
        row(
            "schema",
            f"current={status.current or 'none'} head={status.head or 'none'}"
            + ("" if status.up_to_date else "  (run `cyp db upgrade`)"),
            status.up_to_date,
        )

    typer.echo("Athlete config")
    try:
        cfg = load_athlete_config(athlete_config)
        goal = cfg.goals[0]
        row(
            str(athlete_config),
            f"valid; season {cfg.season.type} from {cfg.season.start}, goal {goal.name!r} "
            f"on {goal.date}, {cfg.availability.weekly_max_minutes} min/wk, "
            f"planner mode {cfg.planner.mode}",
            True,
        )
    except ConfigError as exc:
        problems += 1
        row(str(athlete_config), str(exc).splitlines()[0], False)

    typer.echo("Strava")
    if not settings.strava_enabled:
        row("strava", "disabled (STRAVA_ENABLED=false)")
    else:
        tok = token_status(TokenStore.from_settings(settings))
        if not tok["present"]:
            row("token", "missing (run `cyp auth strava`)", False, warn=True)
        elif not tok["valid"]:
            problems += 1
            row("token", tok["error"], False)
        else:
            expiry = (
                f"expired {-tok['expires_in_s']}s ago (auto-refresh on next call)"
                if tok["expired"]
                else f"valid for {tok['expires_in_s']}s"
            )
            row("token", f"athlete_id={tok['athlete_id']} scope={tok['scope']} {expiry}", True)
        if reachable and status.current is not None:
            with session_factory(engine)() as session:
                cursors = SyncCursorRepo(session)
                after = cursors.get(STRAVA_SOURCE, CURSOR_AFTER)
                last = cursors.get(STRAVA_SOURCE, CURSOR_LAST_SYNC)
                snap_raw = cursors.get(STRAVA_SOURCE, CURSOR_RATE_SNAPSHOT)
            row("cursor activities_after", after or "unset (first sync lists all history)")
            row("last sync", last or "never")
            snap: dict[str, Any] = {}
            if snap_raw:
                try:
                    snap = json.loads(snap_raw)
                except json.JSONDecodeError:
                    snap = {}
            if snap:
                row(
                    "rate budget (last run)",
                    f"used {snap.get('usage_15m')}/{snap.get('limit_15m')} per 15 min, "
                    f"{snap.get('remaining_15m')} remaining at the time; "
                    f"daily {snap.get('usage_daily')}/{snap.get('limit_daily')}",
                )
            else:
                row("rate budget (last run)", "no snapshot yet")

    typer.echo("intervals.icu")
    problems += _doctor_intervals(settings, row, engine, reachable and status.current is not None)

    typer.echo("Matching")
    if reachable and status.current is not None:
        _doctor_matching(row, engine)
    else:
        row("activities", "unavailable (schema not migrated)")

    typer.echo("Last job runs")
    if reachable and status.current is not None:
        with session_factory(engine)() as session:
            latest = JobRunRepo(session).latest_per_job()
        unified = [latest[j] for j in (JOB_SYNC, JOB_BACKFILL) if j in latest]
        if unified:
            lu = max(unified, key=lambda r: (r.started_at, r.id))
            row(
                "last unified sync",
                f"{lu.job} {lu.status} started={lu.started_at} finished={lu.finished_at}",
                lu.status == "ok",
                warn=True,
            )
        else:
            row("last unified sync", "never (run `cyp sync` or `cyp backfill --days N`)")
        for job in JOBS:
            run = latest.get(job)
            if run is None:
                row(job, "never run")
            else:
                row(job, f"{run.status} started={run.started_at} finished={run.finished_at}")
    else:
        row("job_runs", "unavailable (schema not migrated)")

    typer.echo(f"\n{'all checks passed' if problems == 0 else f'{problems} problem(s)'}")
    raise typer.Exit(code=0 if problems == 0 else 1)


def _doctor_intervals(
    settings: Settings, row: Callable[..., None], engine: Engine, db_ready: bool
) -> int:
    """``cyp doctor`` rows for intervals.icu: key, athlete id from last sync, cursors, failures.

    Returns the number of problems found (missing key is a problem; never-synced is a warning).
    """
    if not settings.intervals_api_key.get_secret_value():
        row("api key", "INTERVALS_API_KEY missing (Settings -> Developer on intervals.icu)", False)
        return 1
    row("api key", "present", True)
    if not db_ready:
        row("cursors", "unavailable (schema not migrated)")
        return 0
    with session_factory(engine)() as session:
        cursors = SyncCursorRepo(session)
        athlete_id = cursors.get(ICU_SOURCE, ICU_CURSOR_ATHLETE_ID)
        newest = cursors.get(ICU_SOURCE, ICU_CURSOR_ACTIVITIES)
        wellness = cursors.get(ICU_SOURCE, ICU_CURSOR_WELLNESS)
        latest = JobRunRepo(session).latest_per_job()
    if athlete_id:
        row("athlete id", athlete_id, True)
    else:
        row("athlete id", "unresolved (resolved from GET /athlete/0 on first `cyp sync intervals`)")
    row("cursor activities_newest", newest or "unset (first sync imports the initial window)")
    row("cursor wellness_newest", wellness or "unset")
    failed = sorted(
        job
        for job, run in latest.items()
        if (job.startswith("sync:icu:") or job == "backfill:icu") and run.status == "failed"
    )
    if failed:
        row("last runs", f"failed: {', '.join(failed)}", False, warn=True)
    return 0


def _doctor_matching(row: Callable[..., None], engine: Engine) -> None:
    """``cyp doctor`` rows for the Strava <-> icu matcher: rows by method, rides w/o streams."""
    from sqlalchemy import func

    with session_factory(engine)() as session:
        by_method = Matcher.rows_by_method(session)
        total = sum(by_method.values())
        rides = session.scalar(
            select(func.count()).select_from(Activity).where(Activity.is_ride.is_(True))
        )
        no_streams = session.scalar(
            select(func.count())
            .select_from(Activity)
            .outerjoin(StreamFile, StreamFile.activity_id == Activity.id)
            .where(Activity.is_ride.is_(True), StreamFile.activity_id.is_(None))
        )
        icu_only = session.scalar(
            select(func.count())
            .select_from(Activity)
            .where(Activity.intervals_id.is_not(None), Activity.strava_id.is_(None))
        )
    methods = ", ".join(f"{m}={n}" for m, n in sorted(by_method.items())) or "none"
    row("activities", f"{total} rows ({methods})")
    row(
        "rides without streams",
        f"{no_streams or 0} of {rides or 0}",
        (no_streams or 0) == 0,
        warn=True,
    )
    row("icu rows without a Strava id", str(icu_only or 0))


# ------------------------------------------------------------------------------------------ db


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations up to head."""
    settings = _settings()
    upgrade_head(settings.cyp_db_url)
    engine = engine_from_settings(settings)
    status = schema_status(engine, settings.cyp_db_url)
    typer.echo(f"schema at {status.current}")
    log.info("db.upgrade", revision=status.current)


# ------------------------------------------------------------------------------------- explain


def _find_explanation(settings: Settings, key: str) -> tuple[str, dict[str, Any]] | None:
    """Scan the four explanation-bearing tables for an ``Explanation`` with ``key``."""
    engine = engine_from_settings(settings)
    with session_factory(engine)() as session:
        for model in EXPLANATION_TABLES:
            col = model.explanation  # type: ignore[attr-defined]
            stmt = select(col).where(col.is_not(None))
            for blob in session.scalars(stmt):
                if isinstance(blob, dict) and blob.get("key") == key:
                    return model.__tablename__, blob
    from cyp.analysis.longitudinal.run import load_latest_report

    report = load_latest_report(settings.reports_dir)
    for blob in (report or {}).get("explanations", []):
        if isinstance(blob, dict) and blob.get("key") == key:
            return "reports/trends/latest.json", blob
    return None


@app.command()
def explain(
    key: Annotated[str, typer.Argument(help="Explanation key, e.g. readiness.verdict")],
) -> None:
    """Print the persisted Explanation for KEY (stub: full rendering arrives in M5)."""
    settings = _settings()
    found = _find_explanation(settings, key)
    if found is None:
        typer.echo(f"no explanation found for key {key!r}")
        raise typer.Exit(code=1)
    table, blob = found
    typer.echo(f"# {key}  (from {table})")
    typer.echo(json.dumps(blob, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------------------- stubs


def _not_implemented(name: str) -> None:
    _settings()
    log.info("not implemented in M0", command=name)
    typer.echo(f"cyp {name}: not implemented in M0")


NoStreamsOpt = Annotated[
    bool, typer.Option("--no-streams", help="Skip per-activity stream downloads.")
]
NoWaitOpt = Annotated[
    bool,
    typer.Option(
        "--no-wait", help="On Strava rate limit: persist the cursor and exit instead of sleeping."
    ),
]


@sync_app.callback()
def sync(ctx: typer.Context, no_streams: NoStreamsOpt = False, no_wait: NoWaitOpt = False) -> None:
    """Unified incremental sync: intervals.icu -> matcher -> Strava -> matcher."""
    if ctx.invoked_subcommand is not None:
        return
    _run_unified(no_streams=no_streams, no_wait=no_wait)


@app.command()
def backfill(
    days: Annotated[
        int, typer.Option("--days", min=1, help="Days of history to import (paged by month).")
    ],
    no_streams: NoStreamsOpt = False,
    no_wait: NoWaitOpt = False,
) -> None:
    """History import: intervals.icu backfill -> matcher -> Strava -> matcher (resumable)."""
    _run_unified(backfill_days=days, no_streams=no_streams, no_wait=no_wait)


def _run_unified(*, backfill_days: int | None = None, no_streams: bool, no_wait: bool) -> None:
    settings = _settings()
    engine = engine_from_settings(settings)
    try:
        status = schema_status(engine, settings.cyp_db_url)
    finally:
        engine.dispose()
    if not status.up_to_date:
        typer.echo("database schema is not at head (run `cyp init` and `cyp db upgrade`)", err=True)
        raise typer.Exit(code=2)
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


def _fmt_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"


def _echo_sync_result(result: SyncResult) -> None:
    typer.echo("intervals.icu")
    for name, counts in result.intervals.items():
        typer.echo(f"  {name}: {_fmt_counts(counts)}")
    typer.echo(f"match (after intervals): {_fmt_counts(result.match_before_strava)}")
    typer.echo("strava")
    if result.strava is None:
        typer.echo(f"  skipped ({result.strava_skipped_reason or 'not run'})")
    else:
        typer.echo(f"  {_fmt_counts(result.strava)}")
        if result.strava_remaining_15m is not None:
            typer.echo(f"  remaining_15m: {result.strava_remaining_15m}")
    if result.match_after_strava is not None:
        typer.echo(f"match (after strava): {_fmt_counts(result.match_after_strava)}")
    for err in result.errors:
        typer.echo(f"  error: {err}")
    log.info("sync.unified.done", job=result.job, stages=result.stages_run, errors=result.errors)


# ------------------------------------------------------------------------------- strava (M1)


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
    settings = _settings()
    store = TokenStore.from_settings(settings)
    try:
        if import_from is not None:
            token = import_token_file(import_from, store)
        else:
            token = run_local_callback_flow(
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
    settings = _settings()
    if not settings.strava_enabled:
        typer.echo("strava sync skipped (STRAVA_ENABLED=false)")
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
    typer.echo(f"  match: {_fmt_counts(match.counts())}")
    typer.echo(
        f"  cursor: {summary.cursor_before} -> {summary.cursor_after}"
        f"  remaining_15m: {summary.rate_limit.get('remaining_15m')}"
    )
    for err in summary.errors:
        typer.echo(f"  error: {err}")
    if summary.rate_limited:
        typer.echo("stopped: rate limited (cursor persisted; re-run after the window resets)")
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------- intervals (M1)


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
    no_streams: Annotated[
        bool, typer.Option("--no-streams", help="Skip per-activity stream downloads.")
    ] = False,
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

    settings = _settings()
    key = settings.intervals_api_key.get_secret_value()
    if not key:
        typer.echo("INTERVALS_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=2)
    stages = tuple(stage) if stage else STAGES
    unknown = [st for st in stages if st not in STAGES]
    if unknown:
        typer.echo(
            f"unknown stage(s): {', '.join(unknown)}; expected {', '.join(STAGES)}", err=True
        )
        raise typer.Exit(code=2)
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
        typer.echo(f"  {name}: {_fmt_counts(counts)}")
    typer.echo(f"  match: {_fmt_counts(match.counts())}")
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

    settings = _settings()
    key = settings.intervals_api_key.get_secret_value()
    if not key:
        typer.echo("INTERVALS_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=2)
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
    typer.echo(f"refetch-streams: {_fmt_counts(counts)}")
    typer.echo("next: `cyp analyze` to recompute climbs/fingerprints from the new files")


def _today(settings: Settings) -> dt.date:
    from cyp.core.timeutil import local_date, now_utc

    return local_date(now_utc(), settings.cyp_timezone)


def _season_phase(day: dt.date) -> str | None:
    """Phase of ``day`` from the athlete config (``None`` before the season / without config)."""
    try:
        cfg = load_athlete_config(DEFAULT_ATHLETE_CONFIG)
    except ConfigError:
        return None
    return "base" if day >= cfg.season.start else None


DateOpt = Annotated[
    str | None, typer.Option("--date", help="Local date YYYY-MM-DD (default: today).")
]


def _parse_day(settings: Settings, value: str | None) -> dt.date:
    if value is None:
        return _today(settings)
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        typer.echo(f"invalid --date {value!r}; expected YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def analyze(
    force: Annotated[
        bool, typer.Option("--force", help="Recompute every ride with streams, ignoring versions.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Analyse at most N activities.")
    ] = None,
    activity: Annotated[
        list[int] | None,
        typer.Option("--activity", help="Analyse only these activity ids (repeatable)."),
    ] = None,
    rides_only: Annotated[
        bool,
        typer.Option("--rides-only", help="Stop after per-ride metrics (skip trends + readiness)."),
    ] = False,
) -> None:
    """Per-ride metrics, then longitudinal trends, then today's readiness."""
    from cyp.analysis.run import ALGO_VERSION, analyze_pending
    from cyp.store.streams import StreamStore

    settings = _settings()
    engine = engine_from_settings(settings)
    factory = session_factory(engine)
    try:
        summary = analyze_pending(
            factory,
            store=StreamStore(settings.streams_dir),
            limit=limit,
            force=force,
            activity_ids=activity,
            log_path=str(settings.logs_dir / "cyp.jsonl"),
        )
        typer.echo(f"analyze (algo {ALGO_VERSION}, run {summary.run_id})")
        for key, value in sorted(summary.counts().items()):
            typer.echo(f"  {key}: {value}")
        for r in summary.results:
            if r.outcome == "analyzed" and r.metrics is not None:
                m = r.metrics
                tss = f"{m.tss:.0f}" if m.tss is not None else "-"
                np_w = f"{m.np_w:.0f}" if m.np_w is not None else "-"
                typer.echo(
                    f"  ride:{r.activity_id}  TSS {tss} ({m.tss_source})  NP {np_w}  "
                    f"{m.classification}  {m.status}/{m.next_recommendation}"
                )
            elif r.outcome == "failed":
                typer.echo(f"  ride:{r.activity_id}  FAILED {r.error}")
        if not rides_only:
            day = _today(settings)
            _run_trends(settings, factory, day)
            _run_readiness(factory, [day])
    finally:
        engine.dispose()
    if any(r.outcome == "failed" for r in summary.results):
        raise typer.Exit(code=1)


def _run_trends(settings: Settings, factory: Any, day: dt.date) -> Any:
    from cyp.analysis.longitudinal.run import build_trends

    try:
        report = build_trends(
            factory,
            StreamStore(settings.streams_dir),
            as_of=day,
            phase=_season_phase(day),
            reports_dir=settings.reports_dir,
        )
    except CypError as exc:
        typer.echo(f"trends failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_trends(report)
    return report


def _echo_trends(report: Any) -> None:
    typer.echo(f"trends as of {report.as_of} (FTP {report.ftp or '-'} W)")
    p = report.pmc_today
    if p:
        icu = f"  icu CTL {p['ctl_icu']:.1f}" if p.get("ctl_icu") is not None else ""
        acwr = f"{p['acwr_7_28']:.2f}" if p.get("acwr_7_28") is not None else "-"
        ramp = f"{p['ramp_rate']:+.1f}" if p.get("ramp_rate") is not None else "-"
        typer.echo(
            f"  PMC  CTL {p['ctl']:.1f}  ATL {p['atl']:.1f}  TSB {p['tsb']:+.1f}  "
            f"ramp {ramp}  ACWR {acwr}{icu}"
        )
    a = report.pmc_agreement
    if a:
        flag = "OK" if a["within_tolerance"] else "OUT OF ±1"
        typer.echo(
            f"  PMC vs icu ({a['decay']}): max |ΔCTL| {a['max_abs_ctl_err']:.2f} over "
            f"{a['n_days']} d  [{flag}]"
        )
    for window, entry in report.cp_fits.items():
        for model in ("cp_2p", "cp_3p"):
            fit = entry.get(model)
            if not fit:
                continue
            diff = fit.get("cp_diff_vs_icu_pct")
            vs = f"  vs icu {diff:+.1f} %" if diff is not None else ""
            pmax = f"  Pmax {fit['p_max']}" if fit.get("p_max") else ""
            typer.echo(
                f"  {window} {model}: CP {fit['cp']:.0f} W  W' {fit['w_prime'] / 1000:.1f} kJ"
                f"{pmax}  r2 {fit['r2']}{vs}"
            )
    fp = report.ftp_proposal
    if fp:
        if fp["proposed_ftp"]:
            typer.echo(
                f"  FTP proposal: {fp['current_ftp']:.0f} -> {fp['proposed_ftp']:.0f} W "
                f"({fp['change_pct']:+.1f} %, {fp['days_sustained']} d) — NOT applied"
            )
        elif fp.get("unsupported"):
            typer.echo(
                f"  FTP proposal: withheld — estimates up for {fp['days_sustained']} d but best "
                f"20 min {fp['best_20min_w']:.0f} W does not support it"
            )
        else:
            typer.echo(f"  FTP proposal: none ({fp['days_sustained']} d beyond ±3 %)")
    blocks = [b for b in report.durability_blocks if b.get("median_ratio")]
    if blocks:
        b = blocks[-1]
        typer.echo(
            f"  durability (EF late/fresh, {b['end']}): {b['median_ratio']:.3f} "
            f"over {b['n_long']} long rides"
        )
    if report.tid_weeks:
        w = report.tid_weeks[-1]
        typer.echo(
            f"  TID week {w['week_start']}: {w['low'] * 100:.0f}/{w['mid'] * 100:.0f}/"
            f"{w['high'] * 100:.0f} %  {w['hours']} h  {w['model']}"
        )
    if report.climbs:
        typer.echo(f"  repeat climbs: {len(report.climbs)} (top: {report.climbs[0]['n']} efforts)")
    for lim in report.limiters:
        typer.echo(f"  limiter {lim['id']} ({lim['severity']:.2f}): {lim['title_zh']}")


def _run_readiness(factory: Any, days: list[dt.date]) -> list[Any]:
    from cyp.analysis.readiness_job import run_readiness

    try:
        verdicts = run_readiness(factory, days)
    except CypError as exc:
        typer.echo(f"readiness failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for r in verdicts:
        missing = ",".join(r.inputs.get("missing", [])) or "-"
        typer.echo(
            f"readiness {r.date_local}: {r.score_0_100:.0f} {r.status}/{r.recommendation}  "
            f"(missing: {missing})"
        )
        if r.explanation is not None:
            typer.echo(f"  {r.explanation.headline_zh}")
    return verdicts


@app.command()
def trends(date: DateOpt = None) -> None:
    """Longitudinal trends: PMC replay vs icu, CP/W', FTP proposal, durability, TID, limiters."""
    settings = _settings()
    engine = engine_from_settings(settings)
    try:
        _run_trends(settings, session_factory(engine), _parse_day(settings, date))
    finally:
        engine.dispose()


@app.command()
def readiness(
    date: DateOpt = None,
    days: Annotated[
        int, typer.Option("--days", min=1, help="Also (re)compute the N-1 days before --date.")
    ] = 1,
    coverage: Annotated[
        bool, typer.Option("--coverage", help="Show which wellness fields the store holds.")
    ] = False,
) -> None:
    """Daily readiness verdict (rules + weighted z-scores) with its explanation."""
    settings = _settings()
    engine = engine_from_settings(settings)
    factory = session_factory(engine)
    day = _parse_day(settings, date)
    try:
        if coverage:
            _echo_wellness_coverage(factory, day)
        _run_readiness(factory, [day - dt.timedelta(days=i) for i in range(days - 1, -1, -1)])
    finally:
        engine.dispose()


def _echo_wellness_coverage(factory: Any, day: dt.date) -> None:
    from cyp.analysis.readiness import WELLNESS_FIELDS, wellness_coverage
    from cyp.store.models import WellnessDaily

    with factory() as s:
        rows = s.scalars(
            select(WellnessDaily).where(
                WellnessDaily.date_local > day - dt.timedelta(days=60),
                WellnessDaily.date_local <= day,
            )
        ).all()
        dicts = [{f: getattr(w, f) for f in WELLNESS_FIELDS} for w in rows]
    counts = wellness_coverage(dicts)
    typer.echo(f"wellness coverage (last 60 d, {len(rows)} rows):")
    for f, n in counts.items():
        typer.echo(f"  {f:14s} {n:3d}")


@app.command()
def plan() -> None:
    """Replan the rolling horizon and show/apply the diff (M4)."""
    _not_implemented("plan")


@app.command()
def daily() -> None:
    """Unattended daily pipeline: sync, analyze, readiness, replan, publish, report."""
    _not_implemented("daily")


@app.command()
def weekly() -> None:
    """Weekly review: FTP/eFTP, block progression, next-week template, report."""
    _not_implemented("weekly")


if __name__ == "__main__":  # pragma: no cover
    app()
