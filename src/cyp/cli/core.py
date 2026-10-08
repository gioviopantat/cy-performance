"""Core commands: ``init``, ``doctor``, ``db upgrade``, ``explain``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated, Any

import typer
from sqlalchemy import Engine, select

from cyp.cli.common import (
    AthleteConfigOpt,
    app_context,
    cli_settings,
    log,
)
from cyp.core.errors import ConfigError
from cyp.ingest.intervals.sync import CURSOR_ACTIVITIES as ICU_CURSOR_ACTIVITIES
from cyp.ingest.intervals.sync import CURSOR_ATHLETE_ID as ICU_CURSOR_ATHLETE_ID
from cyp.ingest.intervals.sync import CURSOR_WELLNESS as ICU_CURSOR_WELLNESS
from cyp.ingest.intervals.sync import SOURCE as ICU_SOURCE
from cyp.ingest.matcher import JOB as MATCH_JOB
from cyp.ingest.matcher import Matcher
from cyp.ingest.strava.oauth import TokenStore, token_status
from cyp.ingest.strava.sync import CURSOR_AFTER, CURSOR_LAST_SYNC, CURSOR_RATE_SNAPSHOT
from cyp.ingest.strava.sync import SOURCE as STRAVA_SOURCE
from cyp.jobs.sync import JOB_BACKFILL, JOB_STRAVA, JOB_SYNC
from cyp.services.context import NoDataError
from cyp.settings import Settings, get_settings, load_athlete_config
from cyp.store.db import engine_from_settings, ping, session_factory
from cyp.store.migrate import schema_status, upgrade_head
from cyp.store.models import Activity, StreamFile
from cyp.store.repo.job_runs import JobRunRepo
from cyp.store.repo.sync_cursors import SyncCursorRepo

db_app = typer.Typer(help="Database schema management.", no_args_is_help=True)

JOBS = (
    "daily",
    "weekly",
    JOB_SYNC,
    JOB_BACKFILL,
    MATCH_JOB,
    JOB_STRAVA,
    "analyze",
    "plan",
    "autopilot",
)

Row = Callable[..., None]


def init() -> None:
    """Create the data directory layout (streams/, tokens/, logs/, reports/)."""
    from cyp.logging import configure_logging

    settings = get_settings()
    for path in settings.data_layout:
        path.mkdir(parents=True, exist_ok=True)
    settings.tokens_dir.chmod(0o700)
    configure_logging(settings.logs_dir, console=False)
    typer.echo(f"data dir ready: {settings.cyp_data_dir.resolve()}")
    for path in settings.data_layout[1:]:
        typer.echo(f"  {path.relative_to(settings.cyp_data_dir)}/")
    log.info("init.done", data_dir=str(settings.cyp_data_dir))


def _row(label: str, value: object, ok: bool | None = None, *, warn: bool = False) -> None:
    if ok is None:
        mark = ""
    elif ok:
        mark = "OK   "
    else:
        mark = "WARN " if warn else "FAIL "
    typer.echo(f"  {mark}{label}: {value}")


def doctor(athlete_config: AthleteConfigOpt = None) -> None:
    """Report settings (masked), data dir, DB + schema, athlete config, tokens, last job runs."""
    settings = get_settings()
    athlete_config = athlete_config or settings.cyp_athlete_config
    problems = 0
    row = _row

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
    db_ready = False
    if reachable:
        status = schema_status(engine, settings.cyp_db_url)
        problems += not status.up_to_date
        db_ready = status.current is not None
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
    problems += _doctor_strava(settings, row, engine, db_ready)

    typer.echo("intervals.icu")
    problems += _doctor_intervals(settings, row, engine, db_ready)

    typer.echo("Matching")
    if db_ready:
        _doctor_matching(row, engine)
    else:
        row("activities", "unavailable (schema not migrated)")

    typer.echo("Last job runs")
    if db_ready:
        _doctor_jobs(row, engine)
    else:
        row("job_runs", "unavailable (schema not migrated)")

    typer.echo(f"\n{'all checks passed' if problems == 0 else f'{problems} problem(s)'}")
    raise typer.Exit(code=0 if problems == 0 else 1)


def _doctor_strava(settings: Settings, row: Row, engine: Engine, db_ready: bool) -> int:
    """``cyp doctor`` rows for Strava: token, cursors, last rate-limit snapshot."""
    if not settings.strava_enabled:
        row("strava", "disabled (feature sync.strava off)")
        return 0
    problems = 0
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
    if not db_ready:
        return problems
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
    return problems


def _doctor_intervals(settings: Settings, row: Row, engine: Engine, db_ready: bool) -> int:
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


def _doctor_matching(row: Row, engine: Engine) -> None:
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


def _doctor_jobs(row: Row, engine: Engine) -> None:
    """``cyp doctor`` rows for the last run of every job."""
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


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations up to head."""
    settings = cli_settings()
    upgrade_head(settings.cyp_db_url)
    engine = engine_from_settings(settings)
    try:
        status = schema_status(engine, settings.cyp_db_url)
    finally:
        engine.dispose()
    typer.echo(f"schema at {status.current}")
    log.info("db.upgrade", revision=status.current)


def explain(
    key: Annotated[str, typer.Argument(help="Explanation key, e.g. readiness.verdict")],
) -> None:
    """Print the persisted Explanation for KEY."""
    from cyp.services import trends as trends_service

    with app_context(cli_settings()) as ctx:
        try:
            found = trends_service.explain(ctx, key)
        except NoDataError:
            found = None
    if found is None:
        typer.echo(f"no explanation found for key {key!r}")
        raise typer.Exit(code=1)
    typer.echo(f"# {key}  (from {found.source})")
    typer.echo(json.dumps(found.explanation, ensure_ascii=False, indent=2))


def register(app: typer.Typer) -> None:
    """Attach the core commands to ``app``."""
    app.command()(init)
    app.command()(doctor)
    app.add_typer(db_app, name="db")
    app.command()(explain)
