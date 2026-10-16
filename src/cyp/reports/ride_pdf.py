"""Render a :class:`~cyp.reports.ride_model.RideReport` to a zh-TW PDF (docs/specs/ride-report.md).

Stub: the signature is the contract between the builder and the watcher.
"""

from __future__ import annotations

from pathlib import Path

from cyp.reports.ride_model import RideReport


def render_ride_pdf(report: RideReport, out_dir: Path) -> Path:
    """Write ``<out_dir>/<date>_<activity_id>.pdf`` and return its path.

    The facts ``.json`` is written beside it.
    """
    raise NotImplementedError
