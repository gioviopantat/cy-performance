"""``ride-log show|save|push``: a ride's RIDE.LOG, saving an edited one, writing it to Strava."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from cyp.cli.common import app_context, cli_settings, fail
from cyp.core.errors import CypError
from cyp.services.context import AppContext

ridelog_app = typer.Typer(help="RIDE.LOG text of a ride; write it to Strava.", no_args_is_help=True)

ActivityArg = Annotated[
    str, typer.Argument(help="Activity id (cyp), or 'latest' for the newest ride.")
]


def _resolve(ctx: AppContext, ref: str) -> int:
    from cyp.store.models import Activity

    if ref != "latest":
        if not ref.isdigit():
            fail(f"activity {ref!r}: expected a number or 'latest'", code=2)
        return int(ref)
    with ctx.factory() as s:
        found = s.scalars(
            select(Activity.id)
            .where(Activity.is_ride.is_(True))
            .order_by(Activity.start_local.desc())
        ).first()
    if found is None:
        fail("no rides stored yet", code=2)
    return int(found)


@ridelog_app.command("show")
def show(activity: ActivityArg = "latest") -> None:
    """Print the WORKOUT + RIDE.LOG block (deterministic; no poem)."""
    from cyp.services.ride_log import build

    with app_context(cli_settings()) as ctx:
        try:
            typer.echo(build(ctx, _resolve(ctx, activity)).text)
        except CypError as exc:
            fail(str(exc), code=1)


@ridelog_app.command("save")
def save_cmd(
    activity: ActivityArg = "latest",
    text_file: Annotated[
        Path | None, typer.Option("--text-file", help="Edited text (with poem); omit to reset.")
    ] = None,
) -> None:
    """Store the edited RIDE.LOG for a ride (shown by the UI and pushed to Strava)."""
    from cyp.services.ride_log import build, save

    with app_context(cli_settings()) as ctx:
        aid = _resolve(ctx, activity)
        try:
            build(ctx, aid)
            save(ctx, aid, text_file.read_text(encoding="utf-8") if text_file else "")
        except (CypError, OSError) as exc:
            fail(f"ride-log save: {exc}", code=2)
    typer.echo(f"saved RIDE.LOG for activity {aid}" if text_file else f"reset activity {aid}")


@ridelog_app.command("push")
def push(
    activity: ActivityArg = "latest",
    text_file: Annotated[
        Path | None,
        typer.Option("--text-file", help="Full text to write (e.g. with a poem); default: show's."),
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm-write", help="Really change the Strava description.")
    ] = False,
) -> None:
    """Write the RIDE.LOG to the ride's Strava description (preview without --confirm-write)."""
    from cyp.services.ride_log import build
    from cyp.services.strava_write import push as do_push

    with app_context(cli_settings()) as ctx:
        aid = _resolve(ctx, activity)
        try:
            text = text_file.read_text(encoding="utf-8") if text_file else build(ctx, aid).text
            result = do_push(ctx, aid, text, write=confirm)
        except (CypError, OSError) as exc:
            fail(f"ride-log push: {exc}", code=2)
    typer.echo(result.after)
    if result.written:
        typer.echo(f"\nwritten to Strava activity {result.strava_id}")
    elif confirm:
        typer.echo("\nunchanged: Strava already has this text")
    else:
        typer.echo(
            f"\npreview for Strava activity {result.strava_id}; add --confirm-write to write"
        )


def register(app: typer.Typer) -> None:
    """Attach ``ride-log``."""
    app.add_typer(ridelog_app, name="ride-log")
