"""Dry-run preview of tomorrow's plan for the per-ride report (section 8).

Runs the deterministic planner through :func:`cyp.services.plan.preview` (nothing is stored,
nothing is written to the calendar) and compares its pick for ``day`` with the planned workout
stored for that day. The 05:30 autopilot still owns the real replan.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

from cyp.core.explain import Explanation
from cyp.reports.ride_common import texts
from cyp.reports.ride_model import Tomorrow
from cyp.schemas import PlanPreviewRequest
from cyp.services.context import AppContext
from cyp.services.plan import preview
from cyp.store.repo.planned import PlannedWorkoutRepo


def shift_to_tomorrow(sentence: str) -> str:
    """Re-phrase a planner sentence written for "today" so it reads for tomorrow (one pass)."""
    words: dict[str, str] = texts()["tomorrow_shift"]
    pattern = re.compile("|".join(re.escape(w) for w in words))
    return pattern.sub(lambda m: words[m.group(0)], sentence)


def _reasons(expls: Iterable[Explanation | None], day: dt.date) -> list[str]:
    """Why the planner changed ``day``: the reasons of its adaptations, read for tomorrow.

    The adaptation headlines name template ids (``z2_endurance_180 -> recovery_spin``); the
    athlete reads the reasons, whose numbers are the evidence (e.g. actual vs planned TSS).
    """
    suffix = f".{day.isoformat()}"
    out: list[str] = []
    for e in expls:
        if e is None or not e.key.endswith(suffix):
            continue
        texts_zh = [r.text_zh for r in e.because] or [e.headline_zh]
        out += [shift_to_tomorrow(t) for t in texts_zh]
    return out


def tomorrow_preview(ctx: AppContext, day: dt.date) -> Tomorrow | None:
    """Compare what the planner would schedule for ``day`` now with what is stored for it.

    ``write`` is never on. ``None`` when there is no plan to compare.
    """
    with ctx.factory() as s:
        pw = PlannedWorkoutRepo(s).first_live_on(day)
        before = None
        if pw is not None:
            params = (pw.steps or {}).get("params") if isinstance(pw.steps, dict) else None
            before = (pw.name, pw.template_id, dict(params or {}))
    # Plan as tomorrow morning's run would: with ``today=day`` the planner sees the ride just
    # done as "yesterday" (overdone / missed-session rules), like the 05:30 autopilot will.
    plan = preview(ctx, PlanPreviewRequest(today=day))
    planned = next((d for d in plan.days if d.date == day), None)
    if planned is None:
        return None
    after_name = planned.name_zh if planned.template_id else None
    if before is None and after_name is None:
        return None
    after = (planned.template_id, dict(planned.params) if planned.template_id else {})
    changed = (before[1], before[2]) != after if before else True
    reasons = _reasons([*plan.adaptations, *plan.repairs], day)
    if changed and not reasons and planned.explanation is not None:
        reasons.append(shift_to_tomorrow(planned.explanation.headline_zh))
    return Tomorrow(
        day=day,
        before_name=before[0] if before else None,
        after_name=after_name,
        changed=changed,
        reasons_zh=reasons,
    )
