"""Developer commands: ``dev seed``, ``dev bench``, ``dev openapi``."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Annotated

import typer

from cyp.cli.common import (
    SERVICE_ERRORS,
    AthleteConfigOpt,
    JsonOpt,
    app_context,
    cli_settings,
    echo_json,
    fail,
    require_athlete_config,
    today,
)
from cyp.cli.serve import load_create_app
from cyp.store.migrate import schema_status, upgrade_head

dev_app = typer.Typer(help="Developer tools: synthetic data, benchmarks, API schema.")


@dev_app.command("seed")
def dev_seed(
    days: Annotated[
        int, typer.Option("--days", min=7, help="Days of synthetic history ending yesterday.")
    ] = 400,
) -> None:
    """Fill an EMPTY store with a deterministic synthetic athlete (rides, streams, wellness)."""
    from cyp.devtools.synthetic import seed_synthetic
    from cyp.store.models import Athlete

    settings = cli_settings()
    for path in settings.data_layout:
        path.mkdir(parents=True, exist_ok=True)
    with app_context(settings, check_schema=False) as ctx:
        if not schema_status(ctx.engine, settings.cyp_db_url).up_to_date:
            upgrade_head(settings.cyp_db_url)
        with ctx.factory() as s:
            has_athlete = s.query(Athlete).count() > 0
        if has_athlete:
            fail(
                "the store already has an athlete; `cyp dev seed` only fills an empty database "
                f"({settings.cyp_db_url})",
                code=2,
            )
        end = today(settings) - dt.timedelta(days=1)
        try:
            summary = seed_synthetic(ctx.factory, ctx.store, days=days, end=end)
        except ValueError as exc:
            fail(f"dev seed refused: {exc}", code=2)
    typer.echo(
        f"seeded {summary.days} days ({summary.first_day} – {summary.last_day}): "
        f"{summary.rides} rides, {summary.other} other activities"
    )
    typer.echo("next: `cyp trends`, `cyp readiness`, `cyp plan --dry-run`, `cyp serve`")


@dev_app.command("bench")
def dev_bench(
    repeat: Annotated[int, typer.Option("--repeat", min=1, help="Runs per step.")] = 5,
    as_json: JsonOpt = False,
    athlete_config: AthleteConfigOpt = None,
) -> None:
    """Time the recompute paths (trends, readiness, plan, FTP) on the current store."""
    from cyp.devtools.bench import run_bench

    settings = cli_settings()
    cfg = require_athlete_config(settings, athlete_config)
    with app_context(settings, athlete_config) as ctx:
        try:
            ctx.dataset()
            result = run_bench(
                ctx.factory,
                ctx.store,
                cfg,
                today=ctx.today(),
                reports_dir=ctx.reports_dir,
                repeat=repeat,
            )
        except SERVICE_ERRORS as exc:
            fail(f"bench failed: {exc}", code=1)
    if as_json:
        echo_json(result)
        return
    typer.echo(f"{'step':<14s} {'first_ms':>9s} {'median_ms':>10s} {'min_ms':>9s}  (n={repeat})")
    for name, t in result.items():
        typer.echo(f"{name:<14s} {t['first_ms']:>9.1f} {t['median_ms']:>10.1f} {t['min_ms']:>9.1f}")


@dev_app.command("openapi")
def dev_openapi(
    out: Annotated[Path, typer.Option("--out", help="Where to write the OpenAPI JSON.")] = Path(
        "docs/api/openapi.json"
    ),
    athlete_config: AthleteConfigOpt = None,
) -> None:
    """Write the HTTP API's OpenAPI schema (the frontend's type source) to --out."""
    create_app = load_create_app()
    settings = cli_settings()
    with app_context(settings, athlete_config, check_schema=False) as ctx:
        schema = create_app(ctx).openapi()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"wrote {out} ({len(schema.get('paths', {}))} paths)")


def register(app: typer.Typer) -> None:
    """Attach ``dev`` to ``app``."""
    app.add_typer(dev_app, name="dev")
