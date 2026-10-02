"""``cyp`` command-line entry point (docs/01 §5.9).

The commands live in topic modules and call the service layer (:mod:`cyp.services`):

- :mod:`~cyp.cli.core`: ``init``, ``doctor``, ``db upgrade``, ``explain``
- :mod:`~cyp.cli.sync`: ``sync`` (+ ``intervals`` / ``strava`` / ``refetch-streams``),
  ``backfill``, ``auth strava``
- :mod:`~cyp.cli.analysis`: ``analyze``, ``trends``, ``readiness``, ``ftp`` (+ ``accept``)
- :mod:`~cyp.cli.planning`: ``plan``, ``season``, ``publish spike``
- :mod:`~cyp.cli.reports`: ``report daily|weekly``, ``daily``, ``weekly``
- :mod:`~cyp.cli.dev`: ``dev seed``, ``dev bench``, ``dev openapi``
- :mod:`~cyp.cli.serve`: ``serve``
"""

from __future__ import annotations

from typing import Annotated

import typer

from cyp import __version__
from cyp.cli import analysis, core, dev, planning, reports, serve, sync

# Re-exported so ``auth strava`` resolves it here at call time (tests stub the browser flow).
from cyp.ingest.strava.oauth import run_local_callback_flow

__all__ = ["app", "run_local_callback_flow"]

app = typer.Typer(
    name="cyp",
    help="Cycling performance analysis + adaptive plans for intervals.icu.",
    no_args_is_help=True,
    rich_markup_mode=None,
)


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


for _module in (core, sync, analysis, planning, reports, dev, serve):
    _module.register(app)
