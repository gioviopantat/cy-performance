"""Activity list, per-ride detail and chart-ready (downsampled) streams."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np

from cyp.analysis.ride.classify import CLASS_ZH
from cyp.core.errors import NotFoundError
from cyp.core.explain import Explanation
from cyp.dataset import ActivityRow
from cyp.schemas import ActivityDetail, ActivityPage, ActivitySummary, StreamSeries
from cyp.services.context import AppContext
from cyp.store.models import Activity, ActivityMetrics

STREAM_COLUMNS = ("watts", "hr", "cad", "speed_mps", "alt_m", "grade_pct", "lat", "lng", "dist_m")
MAX_POINTS = 5000


def _summary(a: ActivityRow) -> ActivitySummary:
    return ActivitySummary(
        id=a.id,
        date=a.date,
        name=a.name,
        sport=a.sport,
        is_ride=a.is_ride,
        moving_s=a.moving_s,
        load=round(a.load, 1),
        load_source=a.load_source,
        tss=round(a.tss, 1) if a.tss is not None else None,
        np_w=round(a.np_w) if a.np_w else None,
        intensity_factor=round(a.if_, 3) if a.if_ else None,
        ef=round(a.ef, 3) if a.ef else None,
        decoupling_pct=round(a.decoupling_pct, 2) if a.decoupling_pct is not None else None,
        classification=a.classification,
        classification_zh=CLASS_ZH.get(a.classification or "") if a.classification else None,
        status=a.status,
        climbs=len(a.climbs),
    )


def list_activities(
    ctx: AppContext,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    sport: str | None = None,
    rides_only: bool = False,
    classification: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> ActivityPage:
    """Newest first, filtered and paged (served from the cached snapshot)."""
    ds = ctx.dataset()
    rows = [
        a
        for a in reversed(ds.activities)
        if (start is None or a.date >= start)
        and (end is None or a.date <= end)
        and (sport is None or a.sport == sport)
        and (not rides_only or a.is_ride)
        and (classification is None or a.classification == classification)
    ]
    page = rows[offset : offset + limit]
    return ActivityPage(
        total=len(rows), offset=offset, limit=limit, items=[_summary(a) for a in page]
    )


def get_activity(ctx: AppContext, activity_id: int) -> ActivityDetail:
    """Stored analysis of one activity.

    Raises:
        NotFoundError: unknown id.
    """
    ds = ctx.dataset()
    row = next((a for a in ds.activities if a.id == activity_id), None)
    if row is None:
        raise NotFoundError(f"activity {activity_id} not found")
    with ctx.factory() as s:
        act = s.get(Activity, activity_id)
        m = s.get(ActivityMetrics, activity_id)
        assert act is not None
        expl = Explanation.from_json_dict(m.explanation) if m and m.explanation else None
        curve = (
            (m.power_curve or {}).get("watts", {}) if m and isinstance(m.power_curve, dict) else {}
        )
        return ActivityDetail(
            summary=_summary(row),
            distance_m=act.distance_m,
            elev_gain_m=act.elev_gain_m,
            avg_w=act.avg_w,
            avg_hr=act.avg_hr,
            max_hr=act.max_hr,
            vi=m.vi if m else None,
            hr_lag_s=m.hr_lag_s if m else None,
            power_curve={str(k): float(v) for k, v in curve.items()},
            time_in_zone_power=m.time_in_zone_power if m else None,
            time_in_zone_hr=m.time_in_zone_hr if m else None,
            climbs=list(m.climbs or []) if m else [],
            efforts=list(m.efforts or []) if m else [],
            pacing=dict(m.pacing or {}) if m else {},
            durability=m.durability if m else None,
            next_recommendation=m.next_recommendation if m else None,
            explanation=expl,
            has_streams=ctx.store.exists(activity_id),
        )


def streams(ctx: AppContext, activity_id: int, *, resolution_s: int | None = None) -> StreamSeries:
    """Parquet streams averaged into ``resolution_s`` buckets (auto: ≤ 5 000 points).

    lat/lng take the bucket's first sample (averaging GPS would cut corners). NaN -> ``null``.

    Raises:
        NotFoundError: no stream file.
    """
    df = ctx.store.read(activity_id)
    n = df.height
    res = resolution_s or max(1, math.ceil(n / MAX_POINTS))
    idx = np.arange(n) // res
    cols: dict[str, list[float | None]] = {}
    t = df["t_s"].to_numpy()
    cols["t_s"] = [float(v) for v in t[::res]]
    for name in STREAM_COLUMNS:
        if name not in df.columns:
            continue
        v = df[name].cast(float).to_numpy()
        if name in ("lat", "lng"):
            out = v[::res]
        else:
            sums = np.bincount(idx, weights=np.nan_to_num(v))
            counts = np.bincount(idx, weights=np.isfinite(v).astype(float))
            with np.errstate(invalid="ignore", divide="ignore"):
                out = sums / counts
        cols[name] = [
            None if not np.isfinite(x) else round(float(x), 6 if name in ("lat", "lng") else 2)
            for x in out
        ]
    return StreamSeries(activity_id=activity_id, resolution_s=res, n=len(cols["t_s"]), columns=cols)
