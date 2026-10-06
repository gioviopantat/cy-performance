"""``analyze`` job: load each ride's Parquet, resolve the athlete inputs, compute, persist.

- :func:`analyze_activity` handles one activity inside the caller's session (no commit).
- :func:`analyze_pending` drives the queue under ``job_run("analyze")``: every activity with
  ``pending_analysis`` set, every ride whose ``activity_metrics.algo_version`` differs from
  :data:`ALGO_VERSION`, or every ride with streams when ``force``. One session/commit per
  activity so a single bad file never loses the batch.

Input resolution (``RideInputs``), in priority order:

- FTP: ``athlete_settings_history`` row effective on the ride's local date -> the ride's
  ``icu_ftp`` -> latest settings row of any date -> ``None`` (TSS/IF then unavailable).
- Weight / LTHR / max HR / resting HR: same settings row -> ``athletes`` / raw icu payload.
- CP / W': the ride's ``icu_pm_cp`` / ``icu_pm_w_prime`` -> settings row.
- Power zones: settings row (rescaled when its anchor differs from the FTP used) -> icu
  ``icu_power_zones`` (% FTP) from ``raw_intervals_json`` -> Coggan % of FTP.
- HR zones: settings row -> icu ``icu_hr_zones`` (bpm upper bounds) -> none.
- Power quality: ``data_quality.power_unreliable`` rules (:mod:`cyp.core.data_quality`) vs the
  ride's icu ``power_meter_serial`` / gear. A match is persisted in
  ``activity_metrics.comparison`` (``power_unreliable``, ``power_unreliable_rule``) and
  quoted in the ride Explanation. When the rules change, :func:`analyze_pending` re-analyses
  exactly the rides whose stored flag disagrees (no ``ALGO_VERSION`` bump needed).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.ride.frames import load_frame
from cyp.analysis.ride.pipeline import ALGO_VERSION, compute_ride_metrics
from cyp.analysis.ride.power import coggan_zones, zones_from_pct_bounds
from cyp.analysis.ride.result import RideInputs, RideMetrics
from cyp.core.athlete import Zone, ZoneModel
from cyp.core.data_quality import (
    DataQuality,
    PowerRule,
    infer_meter_gears,
    normalise_serial,
    unreliable_rule,
)
from cyp.core.errors import AnalysisError, NotFoundError
from cyp.core.timeutil import local_date, parse_iso
from cyp.jobs.runs import RunContext, job_run
from cyp.logging import get_logger
from cyp.store.models import Activity, ActivityMetrics, Athlete, AthleteSettingsHistory
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.repo.metrics import ActivityMetricsRepo
from cyp.store.streams import StreamStore

log = get_logger(__name__)

__all__ = [
    "ALGO_VERSION",
    "AnalyzeResult",
    "AnalyzeSummary",
    "activity_local_date",
    "analyze_activity",
    "analyze_pending",
    "resolve_inputs",
]

Outcome = Literal["analyzed", "up_to_date", "skipped_not_ride", "skipped_no_streams", "failed"]


@dataclass
class AnalyzeResult:
    """What happened to one activity."""

    activity_id: int
    outcome: Outcome
    metrics: RideMetrics | None = None
    error: str | None = None


@dataclass
class AnalyzeSummary:
    """Batch summary returned by :func:`analyze_pending`."""

    run_id: int | None
    results: list[AnalyzeResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Outcome -> count."""
        out: dict[str, int] = {}
        for r in self.results:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return out


# ------------------------------------------------------------------------------- inputs


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ride_local_date(activity: Activity) -> dt.date:
    if activity.start_local:
        try:
            return dt.datetime.fromisoformat(activity.start_local).date()
        except ValueError:
            pass
    return local_date(parse_iso(activity.start_utc), activity.tz or "Asia/Taipei")


#: Public name for the local calendar day of an activity (longitudinal / readiness use it).
activity_local_date = _ride_local_date


def _zone_model(blob: Any) -> ZoneModel | None:
    if not isinstance(blob, dict):
        return None
    try:
        return ZoneModel.model_validate(blob)
    except ValueError:
        return None


def _rescale_power_zones(zones: ZoneModel, ftp: float) -> ZoneModel:
    """Scale absolute zone bounds from ``zones.anchor`` to ``ftp`` (same % FTP edges)."""
    if abs(zones.anchor - ftp) < 0.5:
        return zones
    k = ftp / zones.anchor
    return ZoneModel(
        kind="power",
        anchor=ftp,
        zones=[
            Zone(
                idx=z.idx,
                name=z.name,
                lo=round(z.lo * k, 1),
                hi=None if z.hi is None else round(z.hi * k, 1),
            )
            for z in zones.zones
        ],
    )


def _hr_zones_from_bounds(lthr: float | None, bounds: Any) -> ZoneModel | None:
    if not isinstance(bounds, list) or not bounds:
        return None
    zones: list[Zone] = []
    lo = 0.0
    for i, b in enumerate(bounds):
        v = _num(b)
        if v is None:
            continue
        hi: float | None = None if v >= 900 else v
        zones.append(Zone(idx=i + 1, name=f"Z{i + 1}", lo=lo, hi=hi))
        if hi is None:
            break
        lo = hi
    if not zones:
        return None
    anchor = lthr if lthr and lthr > 0 else zones[-1].lo
    return ZoneModel(kind="hr", anchor=float(anchor), zones=zones)


def meter_gears(session: Session, dq: DataQuality | None) -> dict[str, frozenset[str]]:
    """Bikes each rule's meter was recorded on (see :func:`infer_meter_gears`)."""
    if dq is None or not dq.power_rules:
        return {}
    rows = session.execute(
        select(
            Activity.start_local,
            Activity.start_utc,
            Activity.tz,
            Activity.gear_id,
            Activity.raw_intervals_json["power_meter_serial"].as_string(),
        ).where(Activity.is_ride.is_(True))
    ).all()
    return infer_meter_gears(
        (
            (local_day_of(sl, su, tz), normalise_serial(serial), gear)
            for sl, su, tz, gear, serial in rows
        ),
        dq.power_rules,
    )


def local_day_of(start_local: str | None, start_utc: str, tz: str | None) -> dt.date:
    """Local day from raw columns (same rule as :func:`activity_local_date`)."""
    if start_local:
        try:
            return dt.datetime.fromisoformat(start_local).date()
        except ValueError:
            pass
    return local_date(parse_iso(start_utc), tz or "Asia/Taipei")


def power_rule_for(
    activity: Activity, dq: DataQuality | None, gears: dict[str, frozenset[str]]
) -> PowerRule | None:
    """The ``power_unreliable`` rule matching ``activity`` (``None`` = power is trusted)."""
    if dq is None or not dq.power_rules or not activity.has_power:
        return None
    raw: dict[str, Any] = activity.raw_intervals_json or {}
    return unreliable_rule(
        dq.power_rules,
        _ride_local_date(activity),
        normalise_serial(raw.get("power_meter_serial")),
        activity.gear_id,
        gears,
    )


def resolve_inputs(
    session: Session,
    activity: Activity,
    *,
    data_quality: DataQuality | None = None,
    gears: dict[str, frozenset[str]] | None = None,
) -> RideInputs:
    """Build :class:`RideInputs` for ``activity`` (see module docstring for the precedence)."""
    raw: dict[str, Any] = activity.raw_intervals_json or {}
    ride_date = _ride_local_date(activity)
    if gears is None:
        gears = meter_gears(session, data_quality)
    settings_repo = AthleteSettingsRepo(session)
    athlete_id = activity.athlete_id
    if athlete_id is None:
        first = session.scalars(select(Athlete.id).order_by(Athlete.id).limit(1)).first()
        athlete_id = int(first) if first is not None else None
    row: AthleteSettingsHistory | None = None
    latest: AthleteSettingsHistory | None = None
    athlete: Athlete | None = None
    if athlete_id is not None:
        row = settings_repo.effective_on(athlete_id, ride_date)
        latest = settings_repo.latest(athlete_id)
        athlete = session.get(Athlete, athlete_id)

    # FTP
    ftp: float | None
    ftp_source: str
    if row is not None and row.ftp:
        ftp, ftp_source = float(row.ftp), f"settings_history:{row.effective_from.isoformat()}"
    elif activity.icu_ftp:
        ftp, ftp_source = float(activity.icu_ftp), "activity.icu_ftp"
    elif _num(raw.get("icu_ftp")):
        ftp, ftp_source = _num(raw.get("icu_ftp")), "raw_intervals_json.icu_ftp"
    elif latest is not None and latest.ftp:
        ftp, ftp_source = float(latest.ftp), "settings_history:latest"
    else:
        ftp, ftp_source = None, "none"

    def pick(*candidates: Any) -> float | None:
        for c in candidates:
            v = _num(c)
            if v is not None and v > 0:
                return v
        return None

    weight = pick(
        row.weight_kg if row else None,
        raw.get("icu_weight"),
        athlete.weight_kg if athlete else None,
        latest.weight_kg if latest else None,
    )
    lthr = pick(row.lthr if row else None, raw.get("lthr"), latest.lthr if latest else None)
    max_hr = pick(
        row.max_hr if row else None, raw.get("athlete_max_hr"), latest.max_hr if latest else None
    )
    resting_hr = pick(
        row.resting_hr if row else None,
        raw.get("icu_resting_hr"),
        latest.resting_hr if latest else None,
    )
    cp = pick(activity.icu_pm_cp, row.eftp if row else None)
    w_prime = pick(activity.icu_pm_w_prime, row.w_prime if row else None)

    # Zones
    power_zones: ZoneModel | None = None
    zones_source = "none"
    if ftp is not None:
        from_row = _zone_model(row.power_zones) if row is not None else None
        if from_row is not None and from_row.kind == "power":
            power_zones, zones_source = _rescale_power_zones(from_row, ftp), "settings_history"
        elif isinstance(raw.get("icu_power_zones"), list):
            power_zones = zones_from_pct_bounds(ftp, raw["icu_power_zones"])
            zones_source = "raw_intervals_json.icu_power_zones"
        if power_zones is None:
            power_zones, zones_source = coggan_zones(ftp), "coggan_pct_ftp"
    hr_zones = _zone_model(row.hr_zones) if row is not None else None
    if hr_zones is None:
        hr_zones = _hr_zones_from_bounds(lthr, raw.get("icu_hr_zones"))

    return RideInputs(
        activity_id=activity.id,
        has_power=bool(activity.has_power),
        has_hr=bool(activity.has_hr),
        race=bool(activity.race),
        trainer=bool(activity.trainer),
        ftp=ftp,
        ftp_source=ftp_source,
        weight_kg=weight,
        lthr=lthr,
        max_hr=max_hr,
        resting_hr=resting_hr,
        cp=cp,
        w_prime=w_prime,
        power_zones=power_zones,
        hr_zones=hr_zones,
        zones_source=zones_source,
        icu_training_load=_num(activity.icu_training_load),
        # Only icu's own NP is a valid cross-check target; ``activities.np_w`` may hold
        # Strava's weighted_average_watts for Strava-only rides.
        icu_np_w=pick(raw.get("icu_weighted_avg_watts")),
        icu_intensity=_num(activity.icu_intensity),
        icu_decoupling=_num(activity.icu_decoupling),
        icu_ftp=_num(activity.icu_ftp),
        power_meter_serial=normalise_serial(raw.get("power_meter_serial")),
        power_unreliable=power_rule_for(activity, data_quality, gears),
    )


# ------------------------------------------------------------------------------- single


def analyze_activity(
    session: Session,
    activity_id: int,
    *,
    store: StreamStore,
    force: bool = False,
    data_quality: DataQuality | None = None,
    gears: dict[str, frozenset[str]] | None = None,
) -> AnalyzeResult:
    """Analyse one activity in ``session`` (caller commits).

    Non-rides are marked done without metrics; rides without a stream file are left pending.

    Raises:
        NotFoundError: unknown activity id.
    """
    activities = ActivityRepo(session)
    activity = activities.get(activity_id)
    if activity is None:
        raise NotFoundError(f"activity {activity_id} not found")
    metrics_repo = ActivityMetricsRepo(session)
    if not activity.is_ride:
        activities.mark_done(activity, "analysis")
        return AnalyzeResult(activity_id, "skipped_not_ride")
    existing = metrics_repo.get(activity_id)
    if (
        existing is not None
        and existing.algo_version == ALGO_VERSION
        and not activity.pending_analysis
        and not force
    ):
        return AnalyzeResult(activity_id, "up_to_date")
    if activities.get_stream_file(activity_id) is None and not store.exists(activity_id):
        return AnalyzeResult(activity_id, "skipped_no_streams")
    try:
        frame = load_frame(store, activity_id, trainer=bool(activity.trainer))
    except NotFoundError:
        return AnalyzeResult(activity_id, "skipped_no_streams")
    inputs = resolve_inputs(session, activity, data_quality=data_quality, gears=gears)
    metrics = compute_ride_metrics(frame, inputs)
    metrics_repo.upsert(activity_id, metrics.to_row())
    activities.mark_done(activity, "analysis")
    log.info(
        "analyze.activity",
        activity_id=activity_id,
        tss=metrics.tss,
        tss_source=metrics.tss_source,
        np_w=metrics.np_w,
        status=metrics.status,
        next=metrics.next_recommendation,
    )
    return AnalyzeResult(activity_id, "analyzed", metrics=metrics)


# ------------------------------------------------------------------------------- batch


def _candidates(session: Session, *, force: bool, limit: int | None) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    if force:
        stmt = (
            select(Activity.id)
            .where(Activity.is_ride.is_(True), Activity.stream_file.has())
            .order_by(Activity.start_utc)
        )
        for aid in session.scalars(stmt):
            if aid not in seen:
                seen.add(aid)
                ids.append(aid)
    else:
        pending = select(Activity.id).where(Activity.pending_analysis.is_(True))
        pending = pending.order_by(Activity.start_utc)
        for aid in session.scalars(pending):
            if aid not in seen:
                seen.add(aid)
                ids.append(aid)
        for act in ActivityMetricsRepo(session).list_stale(ALGO_VERSION):
            if act.id not in seen:
                seen.add(act.id)
                ids.append(act.id)
    return ids[:limit] if limit is not None else ids


def stale_power_flags(
    session: Session, dq: DataQuality | None, gears: dict[str, frozenset[str]]
) -> list[int]:
    """Analysed rides whose stored ``power_unreliable`` flag disagrees with ``dq``."""
    rows = session.execute(
        select(Activity, ActivityMetrics.comparison)
        .join(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
        .where(Activity.is_ride.is_(True))
        .order_by(Activity.start_utc)
    ).all()
    out: list[int] = []
    for activity, comparison in rows:
        stored = bool((comparison or {}).get("power_unreliable"))
        expected = power_rule_for(activity, dq, gears) is not None
        if stored != expected:
            out.append(activity.id)
    return out


def analyze_pending(
    factory: sessionmaker[Session],
    *,
    store: StreamStore,
    limit: int | None = None,
    force: bool = False,
    activity_ids: list[int] | None = None,
    log_path: str | None = None,
    data_quality: DataQuality | None = None,
) -> AnalyzeSummary:
    """Analyse the queue (or ``activity_ids``) under ``job_run("analyze")``; see module doc.

    Rides whose ``power_unreliable`` flag no longer matches ``data_quality`` are re-analysed
    too (forced), so editing ``athlete.yaml`` takes effect on the next run.
    """
    with job_run("analyze", factory, log_path=log_path) as ctx:
        summary = AnalyzeSummary(run_id=ctx.run_id)
        with factory() as session:
            gears = meter_gears(session, data_quality)
            reflag = set(stale_power_flags(session, data_quality, gears))
            if activity_ids is None:
                ids = _candidates(session, force=force, limit=None)
                ids += [i for i in sorted(reflag) if i not in set(ids)]
                ids = ids[:limit] if limit is not None else ids
            else:
                ids = list(activity_ids)
        for aid in ids:
            summary.results.append(
                _analyze_one(
                    factory,
                    aid,
                    store=store,
                    force=force or aid in reflag,
                    ctx=ctx,
                    data_quality=data_quality,
                    gears=gears,
                )
            )
    return summary


def _analyze_one(
    factory: sessionmaker[Session],
    activity_id: int,
    *,
    store: StreamStore,
    force: bool,
    ctx: RunContext,
    data_quality: DataQuality | None = None,
    gears: dict[str, frozenset[str]] | None = None,
) -> AnalyzeResult:
    with factory() as session:
        try:
            result = analyze_activity(
                session,
                activity_id,
                store=store,
                force=force,
                data_quality=data_quality,
                gears=gears,
            )
            session.commit()
        except (AnalysisError, NotFoundError, ValueError, KeyError) as exc:
            session.rollback()
            log.warning("analyze.failed", activity_id=activity_id, error=str(exc))
            result = AnalyzeResult(activity_id, "failed", error=f"{type(exc).__name__}: {exc}")
    ctx.incr(result.outcome)
    return result
