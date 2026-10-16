"""Dry-run preview of tomorrow's plan for the per-ride report (section 8).

Runs the deterministic planner through :func:`cyp.services.plan.preview` (nothing is stored,
nothing is written to the calendar) and compares its pick for ``day`` with the planned workout
stored for that day. The 05:30 autopilot still owns the real replan.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from cyp.reports.ride_model import Tomorrow
from cyp.services.context import AppContext
from cyp.services.plan import preview
from cyp.store.models import PlannedWorkout


def _stored(ctx: AppContext, day: dt.date) -> tuple[str | None, str | None, dict[str, Any]]:
    """(name, template id, params) of the live planned workout stored for ``day``."""
    with ctx.factory() as s:
        pw = s.scalar(
            select(PlannedWorkout)
            .where(
                PlannedWorkout.date_local == day,
                PlannedWorkout.status.not_in(("cancelled", "superseded")),
            )
            .order_by(PlannedWorkout.slot)
        )
        if pw is None:
            return None, None, {}
        params = (pw.steps or {}).get("params") if isinstance(pw.steps, dict) else None
        return pw.name, pw.template_id, dict(params or {})


def _headlines(expls: list[Any], day: dt.date) -> list[str]:
    suffix = f".{day.isoformat()}"
    return [e.headline_zh for e in expls if e is not None and e.key.endswith(suffix)]


def tomorrow_preview(ctx: AppContext, day: dt.date) -> Tomorrow | None:
    """Compare what the planner would schedule for ``day`` now with what is stored for it.

    ``write`` is never on. ``None`` when there is no plan to compare.
    """
    before_name, before_tid, before_params = _stored(ctx, day)
    plan = preview(ctx)
    planned = next((d for d in plan.days if d.date == day), None)
    if planned is None:
        return None
    after_name = planned.name_zh if planned.template_id else None
    if before_name is None and after_name is None:
        return None
    after_params: Mapping[str, int] = planned.params if planned.template_id else {}
    changed = (before_tid, dict(before_params)) != (planned.template_id, dict(after_params))
    reasons = _headlines([*plan.adaptations, *plan.repairs], day)
    if changed and planned.explanation is not None:
        reasons.append(planned.explanation.headline_zh)
    return Tomorrow(
        day=day,
        before_name=before_name,
        after_name=after_name,
        changed=changed,
        reasons_zh=reasons,
    )
