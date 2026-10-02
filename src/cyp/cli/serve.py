"""``cyp serve``: run the local HTTP API (FastAPI + uvicorn)."""

from __future__ import annotations

import importlib
import ipaddress
import os
from typing import Annotated, Any

import typer

from cyp.cli.common import DEFAULT_ATHLETE_CONFIG, AthleteConfigOpt, cli_settings, fail
from cyp.services.context import AppContext

#: Passes ``--athlete-config`` to :func:`app_factory` in uvicorn's reload worker process.
ATHLETE_CONFIG_ENV = "CYP_ATHLETE_CONFIG"


def load_create_app() -> Any:
    """``cyp.api.app.create_app``, imported lazily; exits 1 when the API is unavailable."""
    try:
        module = importlib.import_module("cyp.api.app")
    except ImportError as exc:
        fail(f"the HTTP API is not available ({exc}); install the `serve` extra", code=1)
    return module.create_app


def app_factory() -> Any:
    """Build the API application from env settings (uvicorn ``factory=True`` target)."""
    settings = cli_settings()
    ctx = AppContext.from_settings(
        settings, athlete_config=os.environ.get(ATHLETE_CONFIG_ENV, str(DEFAULT_ATHLETE_CONFIG))
    )
    return load_create_app()(ctx)


def is_loopback(host: str) -> bool:
    """``True`` for ``localhost`` and loopback addresses."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="TCP port.")] = 8765,
    reload: Annotated[
        bool, typer.Option("--reload/--no-reload", help="Restart on code changes (dev).")
    ] = False,
    athlete_config: AthleteConfigOpt = DEFAULT_ATHLETE_CONFIG,
) -> None:
    """Serve the HTTP API for the web UI (no authentication: keep it on localhost)."""
    load_create_app()  # fail fast with a clean message when the API is missing
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as exc:
        fail(f"uvicorn is not installed ({exc}); install the `serve` extra", code=1)
    if not is_loopback(host):
        typer.echo(
            f"WARNING: binding to {host}: the API has no authentication yet — anyone who can "
            "reach this address can read your data and change your plan",
            err=True,
        )
    os.environ[ATHLETE_CONFIG_ENV] = str(athlete_config)
    typer.echo(f"serving on http://{host}:{port}  (Ctrl+C to stop)")
    if reload:
        uvicorn.run("cyp.cli.serve:app_factory", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(app_factory(), host=host, port=port)


def register(app: typer.Typer) -> None:
    """Attach ``serve`` to ``app``."""
    app.command()(serve)
