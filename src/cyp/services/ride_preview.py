"""Dry-run preview of tomorrow's plan for the per-ride report (section 8).

Stub: the signature is the contract between the watcher and the report builder.
"""

from __future__ import annotations

import datetime as dt

from cyp.reports.ride_model import Tomorrow
from cyp.services.context import AppContext


def tomorrow_preview(ctx: AppContext, day: dt.date) -> Tomorrow | None:
    """Compare what the planner would schedule for ``day`` now with what is stored for it.

    ``write`` is never on. ``None`` when there is no plan to compare.
    """
    raise NotImplementedError
