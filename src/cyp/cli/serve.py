"""``cyp serve``: run the local HTTP API (FastAPI + uvicorn)."""

from __future__ import annotations

import importlib
import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from cyp.cli.common import AthleteConfigOpt, cli_settings, fail
from cyp.services.context import AppContext

#: Passes ``--athlete-config`` to :func:`app_factory` in uvicorn's reload worker process.
ATHLETE_CONFIG_ENV = "CYP_ATHLETE_CONFIG"
#: Passes the bound host to :func:`app_factory` (Host-header check on loopback, ADR-0009).
SERVE_HOST_ENV = "CYP_SERVE_HOST"
#: Host names a loopback server answers to without a token (blocks DNS rebinding).
LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "::1", "[::1]"]


def load_create_app() -> Any:
    """``cyp.api.app.create_app``, imported lazily; exits 1 when the API is unavailable."""
    try:
        module = importlib.import_module("cyp.api.app")
    except ImportError as exc:
        fail(f"the HTTP API is not available ({exc}); install the `serve` extra", code=1)
    return module.create_app


#: The built web UI (``npm --prefix web run build``); served at ``/`` when present.
WEB_DIST = Path("web/dist")


def app_factory() -> Any:
    """Build the API application from env settings (uvicorn ``factory=True`` target)."""
    settings = cli_settings()
    ctx = AppContext.from_settings(
        settings, athlete_config=os.environ.get(ATHLETE_CONFIG_ENV) or None
    )
    from cyp.api.registry import ProfileRegistry

    host = os.environ.get(SERVE_HOST_ENV, "127.0.0.1")
    tokenless_loopback = is_loopback(host) and not settings.cyp_api_token.get_secret_value()
    # Every profile is served (X-CYP-Profile header); the built UI from web/dist at "/".
    return load_create_app()(
        ctx,
        registry=ProfileRegistry(ctx),
        web_dir=WEB_DIST,
        trusted_hosts=LOOPBACK_HOSTS if tokenless_loopback else None,
    )


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
    athlete_config: AthleteConfigOpt = None,
) -> None:
    """Serve the HTTP API and web UI (loopback; other addresses need CYP_API_TOKEN)."""
    load_create_app()  # fail fast with a clean message when the API is missing
    try:
        uvicorn = importlib.import_module("uvicorn")
    except ImportError as exc:
        fail(f"uvicorn is not installed ({exc}); install the `serve` extra", code=1)
    if not is_loopback(host):
        if not cli_settings().cyp_api_token.get_secret_value():
            fail(
                f"refusing to bind to {host} without CYP_API_TOKEN: anyone who can reach it could "
                "read every profile and write calendars (set the token in the root .env)",
                code=2,
            )
        typer.echo(
            f"binding to {host}: every /v1 request needs 'Authorization: Bearer <CYP_API_TOKEN>' "
            "(the bundled web UI does not send it; use it on 127.0.0.1)",
            err=True,
        )
    os.environ[SERVE_HOST_ENV] = host
    if athlete_config is not None:
        os.environ[ATHLETE_CONFIG_ENV] = str(athlete_config)
    typer.echo(f"serving on http://{host}:{port}  (Ctrl+C to stop)")
    if reload:
        uvicorn.run("cyp.cli.serve:app_factory", factory=True, host=host, port=port, reload=True)
    else:
        uvicorn.run(app_factory(), host=host, port=port)


def register(app: typer.Typer) -> None:
    """Attach ``serve`` to ``app``."""
    app.command()(serve)
