"""Helpers shared by every ``cyp`` command module: settings, context, dates, options, output."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import BaseModel

from cyp.core.errors import ConfigError, CypError
from cyp.logging import configure_logging, get_logger
from cyp.services.context import AppContext, NoDataError, SchemaOutdatedError
from cyp.settings import DEFAULT_ATHLETE_CONFIG as DEFAULT_ATHLETE_CONFIG
from cyp.settings import AthleteConfig, Settings, get_settings
from cyp.settings import load_athlete_config as _load_athlete_config

log = get_logger("cyp.cli")

#: Errors a service call can raise that the CLI reports as ``<command> failed: ...`` (exit 1).
SERVICE_ERRORS: tuple[type[Exception], ...] = (CypError, NoDataError)

AthleteConfigOpt = Annotated[
    Path,
    typer.Option("--athlete-config", help="Path to athlete.yaml", show_default=True),
]
DateOpt = Annotated[
    str | None, typer.Option("--date", help="Local date YYYY-MM-DD (default: today).")
]
NoStreamsOpt = Annotated[
    bool, typer.Option("--no-streams", help="Skip per-activity stream downloads.")
]
NoWaitOpt = Annotated[
    bool,
    typer.Option(
        "--no-wait", help="On Strava rate limit: persist the cursor and exit instead of sleeping."
    ),
]
NoSyncOpt = Annotated[
    bool, typer.Option("--no-sync", help="Skip the data sync (analyse what is stored).")
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def cli_settings() -> Settings:
    """Settings from env / ``.env`` with file logging configured (console stays quiet)."""
    settings = get_settings()
    configure_logging(settings.logs_dir, console=False)
    return settings


@contextmanager
def app_context(
    settings: Settings,
    athlete_config: Path | str = DEFAULT_ATHLETE_CONFIG,
    *,
    check_schema: bool = True,
) -> Iterator[AppContext]:
    """An :class:`AppContext` for one command; the engine is disposed on exit.

    Exits 2 with "run `cyp db upgrade`" when migrations are pending (unless ``check_schema``
    is off, for commands that migrate themselves or never touch the DB).
    """
    ctx = AppContext.from_settings(settings, athlete_config=athlete_config)
    try:
        if check_schema:
            try:
                ctx.check_schema()
            except SchemaOutdatedError as exc:
                fail(str(exc), code=2)
        yield ctx
    finally:
        ctx.close()


def athlete_config_or_none(path: Path | str = DEFAULT_ATHLETE_CONFIG) -> AthleteConfig | None:
    """Parsed ``athlete.yaml`` or ``None`` when missing / invalid."""
    try:
        return _load_athlete_config(path)
    except ConfigError:
        return None


def require_athlete_config(path: Path | str) -> AthleteConfig:
    """Parsed ``athlete.yaml``; exits 2 with a hint when it is missing or invalid."""
    cfg = athlete_config_or_none(path)
    if cfg is None:
        fail(f"{path} missing or invalid (run `cyp doctor`)", code=2)
    return cfg


def today(settings: Settings) -> dt.date:
    """Local calendar day in the athlete's time zone."""
    from cyp.core.timeutil import local_date, now_utc

    return local_date(now_utc(), settings.cyp_timezone)


def parse_day(settings: Settings, value: str | None) -> dt.date:
    """``--date`` value (``None`` -> today); exits 2 on anything but ``YYYY-MM-DD``."""
    if value is None:
        return today(settings)
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        typer.echo(f"invalid --date {value!r}; expected YYYY-MM-DD", err=True)
        raise typer.Exit(code=2) from exc


def fail(message: str, *, code: int = 1) -> NoReturn:
    """Print ``message`` to stderr and exit with ``code``."""
    typer.echo(message, err=True)
    raise typer.Exit(code=code)


def fmt_counts(counts: dict[str, int]) -> str:
    """``a=1, b=2`` (sorted) or ``nothing to do``."""
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "nothing to do"


def echo_json(obj: Any) -> None:
    """Print a pydantic model (or a list of them) or plain JSON data, indented, UTF-8."""
    if isinstance(obj, BaseModel):
        typer.echo(obj.model_dump_json(indent=2))
        return
    if isinstance(obj, list) and obj and all(isinstance(o, BaseModel) for o in obj):
        obj = [o.model_dump(mode="json") for o in obj]
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
