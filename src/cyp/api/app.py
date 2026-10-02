"""FastAPI application factory.

Design (docs/08-api.md):

- Thin routers over :mod:`cyp.services`; response models from :mod:`cyp.schemas` are the
  contract (OpenAPI at ``/openapi.json``, docs at ``/docs``; ``cyp dev openapi`` exports it).
- Errors are JSON ``{"error": code, "message": text}``: ``no_data`` 409 (empty store),
  ``not_found`` 404, ``config`` 503 (athlete.yaml), ``analysis`` 422.
- Every successful ``GET /v1/*`` carries an ``ETag`` derived from the data version (+ the
  trends report mtime, path and query); ``If-None-Match`` returns 304, so the UI can poll for
  free.
- Optional bearer token (``CYP_API_TOKEN``) for anything beyond localhost; CORS for the dev
  frontend (``CYP_API_CORS_ORIGINS``).
- Writes are serialised through ``AppContext.write_lock``; long work (sync, analyze, daily)
  runs as background jobs (``POST /v1/jobs/{kind}``, poll ``GET /v1/jobs/{id}``).
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from cyp import __version__
from cyp.api.routes import activities, ftp, jobs, meta, plan, readiness, reports
from cyp.core.errors import AnalysisError, ConfigError, NotFoundError
from cyp.dataset import data_version
from cyp.services.context import AppContext, NoDataError
from cyp.services.jobs import JobManager

API_PREFIX = "/v1"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _etag_seed(c: AppContext) -> str:
    with c.factory() as s:
        version = data_version(s)
    latest = c.reports_dir / "trends" / "latest.json"
    mtime = latest.stat().st_mtime_ns if latest.is_file() else 0
    return f"{version}|{mtime}"


def create_app(ctx: AppContext) -> FastAPI:
    """Build the API around an existing :class:`AppContext`."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield
        app.state.jobs.shutdown()

    app = FastAPI(
        title="cy-performance API",
        version=__version__,
        description="Cycling analysis + training plan backend (read models, what-ifs, jobs).",
        lifespan=lifespan,
    )
    app.state.ctx = ctx
    app.state.jobs = JobManager()
    token = ctx.settings.cyp_api_token.get_secret_value()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ctx.settings.api_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "If-None-Match"],
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
        if request.method != "GET" or not path.startswith(API_PREFIX) or "/jobs" in path:
            return await call_next(request)
        seed = await run_in_threadpool(_etag_seed, ctx)
        etag = (
            '"' + hashlib.sha1(f"{seed}|{path}?{request.url.query}".encode()).hexdigest()[:20] + '"'
        )
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

    for module in (meta, activities, ftp, readiness, plan, reports, jobs):
        app.include_router(module.router, prefix=API_PREFIX)
    return app
