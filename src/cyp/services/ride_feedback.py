"""Post-ride feeling answered on the web page (RPE 1–10, feel 1 strong … 5 weak).

Stored per ride under ``data/ride_feedback/<activity_id>.json``; per field, it wins over the
same field from intervals.icu (as in ``readiness_job._yesterday``) and feeds readiness (flag
``readiness.ride_feel``, docs/specs/personalized-planning §8). Answering in intervals.icu works
too, without this store. With ``report.ride`` on, a saved answer starts that profile's ride
watcher tick in the background, so the rated ride's report goes out at once.

The same file holds the athlete's casual-ride tag (``"casual": true``, docs/specs/
casual-and-strava.md): the ride is then not graded (reports) and counts as not done for the
planner; its load counts as always. Tagging a ride whose report was already emailed sends nothing.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyp.core.errors import ConfigError, NotFoundError
from cyp.dataset import Dataset
from cyp.logging import get_logger
from cyp.services.context import AppContext
from cyp.store.models import Activity

log = get_logger(__name__)


@dataclass(frozen=True)
class RideFeedback:
    """One ride's answers; ``source`` says where they came from."""

    activity_id: int
    rpe: int | None
    feel: int | None
    source: str  # "web" (any field answered here) | "intervals.icu" | "none"
    casual: bool = False


def _path(ctx: AppContext, activity_id: int) -> Path:
    return _dir(ctx) / f"{activity_id}.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _dir(ctx: AppContext) -> Path:
    return ctx.settings.cyp_data_dir / "ride_feedback"


def get(ctx: AppContext, activity_id: int) -> RideFeedback:
    """Per field: the web answer, else intervals.icu's.

    Raises:
        NotFoundError: unknown activity.
    """
    web = _read(_path(ctx, activity_id))
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
    return RideFeedback(activity_id, rpe, feel, source, casual=web.get("casual") is True)


def save(
    ctx: AppContext,
    activity_id: int,
    rpe: int | None,
    feel: int | None,
    casual: bool | None = None,
) -> RideFeedback:
    """Store the answers (``rpe``, ``feel`` ``None`` and not casual deletes the file).

    ``casual=None`` keeps the stored tag.

    Raises:
        ConfigError: out-of-range values. NotFoundError: unknown activity.
    """
    if rpe is not None and not 1 <= rpe <= 10:
        raise ConfigError("RPE must be 1–10")
    if feel is not None and not 1 <= feel <= 5:
        raise ConfigError("feel must be 1–5")
    get(ctx, activity_id)  # 404 before writing
    path = _path(ctx, activity_id)
    if casual is None:
        casual = _read(path).get("casual") is True
    _write(path, rpe, feel, casual)
    if rpe is not None or feel is not None:
        _trigger_watch(ctx)
    return get(ctx, activity_id)


def set_casual(ctx: AppContext, activity_id: int, casual: bool) -> RideFeedback:
    """Tag (or untag) the ride as casual, keeping the web RPE / feel; starts no watcher tick.

    Raises:
        NotFoundError: unknown activity.
    """
    get(ctx, activity_id)  # 404 before writing
    path = _path(ctx, activity_id)
    web = _read(path)
    _write(path, web.get("rpe"), web.get("feel"), casual)
    return get(ctx, activity_id)


def _write(path: Path, rpe: int | None, feel: int | None, casual: bool) -> None:
    if rpe is None and feel is None and not casual:
        path.unlink(missing_ok=True)
        return
    data: dict[str, Any] = {"rpe": rpe, "feel": feel}
    if casual:
        data["casual"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _trigger_watch(ctx: AppContext) -> None:
    """Start a watcher tick without waiting for it; a failure to start only logs."""
    try:
        if not ctx.features().enabled("report.ride"):
            return
        from cyp.jobs.watch import spawn_tick

        spawn_tick(ctx.settings)
    except (ConfigError, OSError) as exc:  # the answer is saved; the 10-min agent sends it
        log.warning("ride_feedback.watch_not_started", error=str(exc))


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


def casual_ids(ctx: AppContext) -> frozenset[int]:
    """Every ride the athlete tagged casual."""
    folder = _dir(ctx)
    if not folder.is_dir():
        return frozenset()
    out = set()
    for f in folder.glob("*.json"):
        if f.stem.isdigit() and _read(f).get("casual") is True:
            out.add(int(f.stem))
    return frozenset(out)


def casual_days(ctx: AppContext, ds: Dataset) -> frozenset[dt.date]:
    """Days on which every ride is tagged casual (the planner's "not done" days)."""
    ids = casual_ids(ctx)
    if not ids:
        return frozenset()
    by_day: dict[dt.date, list[int]] = {}
    for a in ds.activities:
        if a.is_ride:
            by_day.setdefault(a.date, []).append(a.id)
    return frozenset(d for d, rides in by_day.items() if all(i in ids for i in rides))
