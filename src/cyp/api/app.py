"""FastAPI application factory.

Design (docs/08-api.md):

- Thin routers over :mod:`cyp.services`; response models from :mod:`cyp.schemas` are the
  contract (OpenAPI at ``/openapi.json``, docs at ``/docs``; ``cyp dev openapi`` exports it).
- Errors are JSON ``{"error": code, "message": text}``: ``no_data`` 409 (empty store),
  ``not_found`` 404, ``config`` 503 (athlete.yaml), ``analysis`` 422.
- Every successful ``GET /v1/*`` carries an ``ETag`` derived from the data version, the latest
  job run, report-file and athlete.yaml mtimes, the local date, profile, path and query;
  ``If-None-Match`` returns 304, so the UI can poll for free. File-backed or live endpoints
  (jobs, autopilot, profiles, ride-log, feedback) are never cached.
- Bearer token (``CYP_API_TOKEN``, required by ``cyp serve`` beyond loopback); without one a
  loopback server checks the ``Host`` header (ADR-0009). CORS for the dev frontend
  (``CYP_API_CORS_ORIGINS``).
- Writes are serialised through ``AppContext.write_lock``; long work (sync, analyze, daily)
  runs as background jobs (``POST /v1/jobs/{kind}``, poll ``GET /v1/jobs/{id}``).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from cyp import __version__
from cyp.api.registry import PROFILE_HEADER, ProfileRegistry
from cyp.api.routes import (
    activities,
    autopilot,
    calendar,
    ftp,
    jobs,
    meta,
    plan,
    profiles,
    readiness,
    reports,
)
from cyp.core.errors import AnalysisError, ConfigError, NotFoundError
from cyp.dataset import data_version
from cyp.services.context import AppContext, NoDataError, SchemaOutdatedError
from cyp.services.jobs import JobManager
from cyp.store.models import JobRun

API_PREFIX = "/v1"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _etag_seed(c: AppContext) -> str:
    """Everything a cached GET can depend on.

    DB data, job history, athlete.yaml, report files and the local date (``today`` defaults
    roll over at midnight).
    """
    with c.factory() as s:
        version = data_version(s)
        last_job = s.scalar(select(func.max(JobRun.id))) or 0
    reports = c.reports_dir
    dirs = [reports, *(d for d in reports.iterdir() if d.is_dir())] if reports.is_dir() else []
    files = max((_mtime(d) for d in dirs), default=0)
    latest = _mtime(reports / "trends" / "latest.json")
    config = _mtime(Path(c.athlete_config_path))
    return f"{version}|{last_job}|{latest}|{files}|{config}|{c.now_local().date()}"


def create_app(
    ctx: AppContext,
    *,
    registry: ProfileRegistry | None = None,
    web_dir: Path | None = None,
    trusted_hosts: list[str] | None = None,
) -> FastAPI:
    """Build the API around a default :class:`AppContext` (+ per-profile contexts).

    ``web_dir``: a built UI (``web/dist``) served at ``/`` when it exists (spec web-ui).
    ``trusted_hosts``: answer only these ``Host`` headers (``cyp serve`` passes the loopback
    names when it runs without a token: a rebinding web page cannot reach the API, ADR-0009).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        app.state.jobs.shutdown()
        app.state.profiles.close()

    app = FastAPI(
        title="cy-performance API",
        version=__version__,
        description="Cycling analysis + training plan backend (read models, what-ifs, jobs).",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.state.profiles = registry or ProfileRegistry(ctx)
    app.state.jobs = JobManager()
    token = ctx.settings.cyp_api_token.get_secret_value()

    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ctx.settings.api_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "If-None-Match", PROFILE_HEADER],
        expose_headers=["ETag"],
    )

    @app.middleware("http")
    async def auth_and_etag(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if token and path.startswith(API_PREFIX):
            given = request.headers.get("authorization", "")
            if not secrets.compare_digest(given, f"Bearer {token}"):
                return _error(401, "unauthorized", "missing or invalid bearer token")
        # Not derived from the DB version (files, live state): never answered with a 304.
        uncached = ("/jobs", "/autopilot", "/profiles", "/ride-log", "/feedback")
        if (
            request.method != "GET"
            or not path.startswith(API_PREFIX)
            or any(p in path for p in uncached)
        ):
            return await call_next(request)
        profile = request.headers.get(PROFILE_HEADER, "")
        try:
            selected = app.state.profiles.get(profile)
        except NotFoundError as exc:
            return _error(404, "not_found", str(exc))
        seed = await run_in_threadpool(_etag_seed, selected)
        key = f"{seed}|{profile}|{path}?{request.url.query}"
        etag = '"' + hashlib.sha1(key.encode()).hexdigest()[:20] + '"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        response = await call_next(request)
        if response.status_code == 200:
            response.headers["ETag"] = etag
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(NoDataError)
    async def _no_data(_: Request, exc: NoDataError) -> JSONResponse:
        return _error(409, "no_data", str(exc))

    @app.exception_handler(SchemaOutdatedError)
    async def _schema(_: Request, exc: SchemaOutdatedError) -> JSONResponse:
        return _error(503, "schema", str(exc))

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error(404, "not_found", str(exc))

    @app.exception_handler(ConfigError)
    async def _config(_: Request, exc: ConfigError) -> JSONResponse:
        return _error(503, "config", str(exc))

    @app.exception_handler(AnalysisError)
    async def _analysis(_: Request, exc: AnalysisError) -> JSONResponse:
        return _error(422, "analysis", str(exc))

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Liveness probe (no DB access)."""
        return {"status": "ok", "version": __version__}

    for module in (
        meta,
        activities,
        calendar,
        ftp,
        readiness,
        plan,
        reports,
        jobs,
        profiles,
        autopilot,
    ):
        app.include_router(module.router, prefix=API_PREFIX)
    if web_dir is not None and (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
