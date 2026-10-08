"""Post-ride feeling answered on the web page (RPE 1–10, feel 1 strong … 5 weak).

Stored per ride under ``data/ride_feedback/<activity_id>.json``; per field, it wins over the
same field from intervals.icu (as in ``readiness_job._yesterday``) and feeds readiness (flag
``readiness.ride_feel``, docs/specs/personalized-planning §8). Answering in intervals.icu works
too, without this store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cyp.core.errors import ConfigError, NotFoundError
from cyp.services.context import AppContext
from cyp.store.models import Activity


@dataclass(frozen=True)
class RideFeedback:
    """One ride's answers; ``source`` says where they came from."""

    activity_id: int
    rpe: int | None
    feel: int | None
    source: str  # "web" (any field answered here) | "intervals.icu" | "none"


def _dir(ctx: AppContext) -> Path:
    return ctx.settings.cyp_data_dir / "ride_feedback"


def get(ctx: AppContext, activity_id: int) -> RideFeedback:
    """Per field: the web answer, else intervals.icu's.

    Raises:
        NotFoundError: unknown activity.
    """
    path = _dir(ctx) / f"{activity_id}.json"
    web: dict[str, int | None] = {}
    if path.is_file():
        try:
            web = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            web = {}
    with ctx.factory() as s:
        a = s.get(Activity, activity_id)
        if a is None:
            raise NotFoundError(f"no activity {activity_id}")
        icu_rpe, icu_feel = a.icu_rpe, a.feel
    rpe = web.get("rpe") if web.get("rpe") is not None else icu_rpe
    feel = web.get("feel") if web.get("feel") is not None else icu_feel
    if web.get("rpe") is not None or web.get("feel") is not None:
        source = "web"
    else:
        source = "intervals.icu" if rpe is not None or feel is not None else "none"
    return RideFeedback(activity_id, rpe, feel, source)


def save(ctx: AppContext, activity_id: int, rpe: int | None, feel: int | None) -> RideFeedback:
    """Store the answers (both ``None`` deletes them).

    Raises:
        ConfigError: out-of-range values. NotFoundError: unknown activity.
    """
    if rpe is not None and not 1 <= rpe <= 10:
        raise ConfigError("RPE must be 1–10")
    if feel is not None and not 1 <= feel <= 5:
        raise ConfigError("feel must be 1–5")
    get(ctx, activity_id)  # 404 before writing
    path = _dir(ctx) / f"{activity_id}.json"
    if rpe is None and feel is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"rpe": rpe, "feel": feel}), encoding="utf-8")
    return get(ctx, activity_id)


def all_answers(ctx: AppContext) -> dict[int, tuple[float | None, int | None]]:
    """Every web answer, for readiness."""
    out: dict[int, tuple[float | None, int | None]] = {}
    for f in _dir(ctx).glob("*.json") if _dir(ctx).is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out[int(f.stem)] = (data.get("rpe"), data.get("feel"))
        except (ValueError, OSError):
            continue
    return out
