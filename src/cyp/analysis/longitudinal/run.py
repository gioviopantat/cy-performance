"""Longitudinal job: compute every trend model from a :class:`~cyp.dataset.Dataset`, persist.

Split in three so each part is fast and testable:

- :func:`compute_trends` — pure: ``Dataset -> TrendsResult`` (report + rows to persist).
  PMC replay, FTP status (:mod:`.ftp`), durability (from ``activity_metrics.durability``),
  TID, repeat climbs, limiters.
- :func:`persist_trends` — bulk upserts: ``fitness_daily`` sim columns (one SELECT for the
  range, then in-memory updates), ``power_curve_snapshots`` (``source='cyp'``).
- :func:`build_trends` — the job: cached dataset -> lazy durability backfill for rides analysed
  before ``ride-1.1.0`` (reads only those Parquet files, once) -> compute -> persist ->
  ``reports/trends/{as_of}.json`` + ``latest.json`` (``cyp explain`` reads it).

Daily load: Σ over the day's activities (all sports) of icu ``icu_training_load``, else our
``activity_metrics.tss``, else 0 (:attr:`Dataset.loads`).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal import climbs as climbs_mod
from cyp.analysis.longitudinal import durability as dur_mod
from cyp.analysis.longitudinal import limiters as lim_mod
from cyp.analysis.longitudinal import pmc
from cyp.analysis.longitudinal import tid as tid_mod
from cyp.analysis.longitudinal.ftp import FtpStatus, compute_ftp_status
from cyp.analysis.ride.frames import load_frame
from cyp.core.data_quality import DataQuality
from cyp.core.errors import AnalysisError, NotFoundError
from cyp.core.explain import Explanation
from cyp.dataset import CACHE, Dataset, local_day
from cyp.logging import get_logger
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    Athlete,
    FitnessDaily,
    PowerCurveSnapshot,
)
from cyp.store.streams import StreamStore

log = get_logger(__name__)

TRENDS_VERSION = "trends_v2"
DURABILITY_LOOKBACK_DAYS = 168
TID_WEEKS = 12
HISTORY_DAYS = 400


def primary_athlete_id(session: Session) -> int | None:
    """The single athlete this install serves (lowest id with an icu id, else lowest id)."""
    aid = session.scalar(
        select(Athlete.id).where(Athlete.intervals_id.is_not(None)).order_by(Athlete.id)
    )
    return aid if aid is not None else session.scalar(select(Athlete.id).order_by(Athlete.id))


@dataclass
class DayLoad:
    """One activity's contribution to a day."""

    activity_id: int
    load: float
    source: str


def daily_loads(session: Session) -> tuple[dict[dt.date, float], dict[dt.date, list[DayLoad]]]:
    """``{date: total load}`` plus the per-activity breakdown (column-selected query)."""
    rows = session.execute(
        select(
            Activity.id,
            Activity.start_local,
            Activity.start_utc,
            Activity.tz,
            Activity.icu_training_load,
            ActivityMetrics.tss,
        ).outerjoin(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
    ).all()
    totals: dict[dt.date, float] = {}
    detail: dict[dt.date, list[DayLoad]] = {}
    for aid, start_local, start_utc, tz, icu_load, tss in rows:
        if icu_load is not None:
            load, src = float(icu_load), "icu"
        elif tss is not None:
            load, src = float(tss), "cyp"
        else:
            load, src = 0.0, "none"
        d = local_day(start_local, start_utc, tz)
        totals[d] = totals.get(d, 0.0) + load
        detail.setdefault(d, []).append(DayLoad(aid, load, src))
    return totals, detail


@dataclass
class TrendsReport:
    """Everything ``cyp trends`` prints and reports render."""

    as_of: dt.date
    athlete_id: int
    ftp: float | None
    pmc_today: dict[str, Any] | None = None
    pmc_agreement: dict[str, Any] | None = None
    pmc_series: list[dict[str, Any]] = field(default_factory=list)
    cp_fits: dict[str, Any] = field(default_factory=dict)
    ftp_proposal: dict[str, Any] | None = None
    durability_blocks: list[dict[str, Any]] = field(default_factory=list)
    tid_weeks: list[dict[str, Any]] = field(default_factory=list)
    climbs: list[dict[str, Any]] = field(default_factory=list)
    limiters: list[dict[str, Any]] = field(default_factory=list)
    #: Best-effort rules that could not be judged (``status: insufficient_data``).
    limiter_checks: list[dict[str, Any]] = field(default_factory=list)
    max_efforts: dict[str, Any] | None = None
    #: Rides whose power is excluded by ``data_quality.power_unreliable`` (docs/04 §7).
    power_unreliable: dict[str, Any] | None = None
    planner_bias: dict[str, float] = field(default_factory=dict)
    explanations: list[dict[str, Any]] = field(default_factory=list)
    version: str = TRENDS_VERSION

    def add(self, expl: Explanation | None) -> None:
        """Append an Explanation (persisted with the report)."""
        if expl is not None:
            self.explanations.append(expl.to_json_dict())

    def to_json(self) -> dict[str, Any]:
        """JSON-safe dict."""
        out: dict[str, Any] = json.loads(
            json.dumps(asdict(self), default=_default, ensure_ascii=False)
        )
        return out

    def find(self, key: str) -> dict[str, Any] | None:
        """Explanation by key."""
        return next((e for e in self.explanations if e.get("key") == key), None)


def _default(o: Any) -> Any:
    if isinstance(o, dt.date):
        return o.isoformat()
    raise TypeError(type(o).__name__)


@dataclass
class TrendsResult:
    """Pure output of :func:`compute_trends`."""

    report: TrendsReport
    ftp: FtpStatus
    pmc_days: list[pmc.PMCDay] = field(default_factory=list)


def compute_trends(ds: Dataset, as_of: dt.date, *, phase: str | None = None) -> TrendsResult:
    """Every longitudinal model as of ``as_of`` (pure; see module docstring)."""
    ftp_status = compute_ftp_status(ds, as_of)
    ftp = ftp_status.current_ftp
    report = TrendsReport(as_of=as_of, athlete_id=ds.athlete_id, ftp=ftp)
    result = TrendsResult(report=report, ftp=ftp_status)

    # ---- PMC replay, seeded from the earliest icu value in the window
    start = as_of - dt.timedelta(days=HISTORY_DAYS)
    icu = {d: (w.ctl, w.atl) for d, w in ds.wellness.items() if w.ctl is not None and d <= as_of}
    if ds.power_fix_until is not None:
        # icu's ledger is knowingly wrong up to the fix date: seed after it and compare after it.
        icu = {d: v for d, v in icu.items() if d > ds.power_fix_until}
    seed_day = min((d for d in icu if d >= start), default=None)
    if seed_day is not None:
        c0, a0 = icu[seed_day]
        seed = pmc.PMCState(seed_day, float(c0 or 0), float(a0 or 0))
        series, agree = pmc.best_params(seed, ds.loads, as_of, icu)
        result.pmc_days = series
        today = series[-1]
        report.pmc_today = {
            "date": today.date,
            "ctl": round(today.ctl, 2),
            "atl": round(today.atl, 2),
            "tsb": round(today.tsb, 2),
            "ramp_rate": today.ramp_rate,
            "acwr_7_28": today.acwr_7_28,
            "monotony_7": today.monotony_7,
            "strain_7": today.strain_7,
            "ctl_icu": icu.get(as_of, (None, None))[0],
        }
        report.pmc_agreement = {
            "decay": agree.params.decay,
            "n_days": agree.n_days,
            "max_abs_ctl_err": round(agree.max_abs_ctl_err, 3),
            "mean_abs_ctl_err": round(agree.mean_abs_ctl_err, 3),
            "max_abs_atl_err": round(agree.max_abs_atl_err, 3),
            "worst_day": agree.worst_day,
            "within_tolerance": agree.within_tolerance,
        }
        report.pmc_series = [
            {
                "date": d.date,
                "load": round(d.load, 1),
                "ctl": round(d.ctl, 2),
                "atl": round(d.atl, 2),
                "tsb": round(d.tsb, 2),
            }
            for d in series[-56:]
        ]
        report.add(pmc.explain_pmc(today, agree))

    # ---- FTP / power-duration
    for name, wf in ftp_status.windows.items():
        report.cp_fits[name] = wf.to_json()
    for e in ftp_status.explanations:
        report.add(e)
    p = ftp_status.proposal
    if p is not None:
        report.ftp_proposal = {
            "current_ftp": p.current_ftp,
            "proposed_ftp": p.proposed_ftp,
            "direction": p.direction,
            "days_sustained": p.days_sustained,
            "median_estimate": p.median_estimate,
            "change_pct": p.change_pct,
            "best_20min_w": p.best_20min_w,
            "unsupported": p.unsupported,
            "insufficient_evidence": p.insufficient_evidence,
            "sources": p.sources,
        }
    report.max_efforts = ftp_status.max_efforts.to_json()
    flagged = ds.power_unreliable_rides
    if flagged:
        report.power_unreliable = {
            "n_rides": len(flagged),
            "first": min(a.date for a in flagged),
            "last": max(a.date for a in flagged),
            "activity_ids": [a.id for a in flagged],
            "rules": [
                r.to_json() for r in (ds.data_quality.power_rules if ds.data_quality else ())
            ],
        }

    # ---- durability (stored per ride)
    lo = as_of - dt.timedelta(days=DURABILITY_LOOKBACK_DAYS)
    dur = [
        r
        for r in (
            dur_mod.from_json(a.id, a.date, a.durability)
            for a in ds.rides(lo, as_of)
            if a.power_unreliable is None
        )
        if r is not None
    ]
    blocks = dur_mod.durability_trend(dur, end=as_of)
    report.durability_blocks = [asdict(b) for b in blocks]
    report.add(dur_mod.explain_durability(blocks, as_of=as_of))

    # ---- TID
    tid_lo = tid_mod.week_start(as_of) - dt.timedelta(weeks=TID_WEEKS - 1)
    tiz_rides = []
    for a in ds.rides(tid_lo, as_of):
        if a.power_unreliable is None:
            picked = tid_mod.tiz_from_metrics(a.tiz_power, a.tiz_hr, a.icu_zone_times)
        else:  # power zones (ours and icu's) come from the unreliable meter: use HR
            picked = tid_mod.tiz_from_metrics(None, a.tiz_hr, None)
        if picked:
            tiz_rides.append(tid_mod.RideTiz(a.date, picked[0], picked[1]))
    weeks = tid_mod.weekly_tid(tiz_rides)
    for w in weeks:
        low, mid, high = w.fractions()
        report.tid_weeks.append(
            {
                "week_start": w.week_start,
                "hours": round(w.total_s / 3600, 2),
                "low": round(low, 3),
                "mid": round(mid, 3),
                "high": round(high, 3),
                "pi": w.polarization_index,
                "model": w.model,
                "n_rides": w.n_rides,
            }
        )
    if weeks:
        report.add(tid_mod.explain_week(weeks[-1], phase))
    recent = [w for w in weeks if w.week_start >= tid_mod.week_start(as_of) - dt.timedelta(weeks=3)]
    tot = sum(w.total_s for w in recent)
    mid_share = sum(w.mid_s for w in recent) / tot if tot > 0 else None

    # ---- repeat climbs
    boards = climbs_mod.leaderboard(
        (a.id, a.date, climbs_mod.strip_power(a.climbs) if a.power_unreliable else list(a.climbs))
        for a in ds.rides(None, as_of)
    )
    for b in boards[:20]:
        report.climbs.append(
            {
                "fingerprint": b.fingerprint,
                "n": len(b.efforts),
                "distance_m": b.distance_m,
                "gain_m": b.gain_m,
                "best": {
                    "activity_id": b.best.activity_id,
                    "date": b.best.date,
                    "duration_s": b.best.duration_s,
                    "w_kg": b.best.w_kg,
                    "vam_m_h": b.best.vam_m_h,
                },
                "latest": {
                    "activity_id": b.latest.activity_id,
                    "date": b.latest.date,
                    "duration_s": b.latest.duration_s,
                    "w_kg": b.latest.w_kg,
                    "rank": b.latest_rank,
                },
                "wkg_slope_per_30d": b.wkg_slope_per_30d,
            }
        )

    # ---- limiters -> planner bias
    last_ratio = next((b.median_ratio for b in reversed(blocks) if b.median_ratio), None)
    limiters = (
        lim_mod.detect_limiters(
            ftp=ftp,
            mmp=ftp_status.windows["42d"].mmp,
            durability_ratio=last_ratio,
            mid_share_4w=mid_share,
            ctl=report.pmc_today["ctl"] if report.pmc_today else None,
            max_efforts=ftp_status.max_efforts,
        )
        if ftp
        else []
    )
    for lim in limiters:
        if lim.status != "limiter":
            report.limiter_checks.append(
                {"id": lim.id, "status": lim.status, "reason_zh": lim.title_zh}
            )
            report.add(lim.explanation)
            continue
        report.limiters.append(
            {
                "id": lim.id,
                "severity": round(lim.severity, 3),
                "title_zh": lim.title_zh,
                "template_bias": lim.template_bias,
            }
        )
        report.add(lim.explanation)
    report.planner_bias = lim_mod.combined_bias(limiters)
    return result


def persist_trends(session: Session, result: TrendsResult, *, weight_kg: float | None) -> None:
    """Bulk-write the simulated fitness rows and our power-curve snapshots (no commit)."""
    athlete_id = result.report.athlete_id
    days = result.pmc_days
    if days:
        existing = {
            r.date_local: r
            for r in session.scalars(
                select(FitnessDaily).where(
                    FitnessDaily.athlete_id == athlete_id,
                    FitnessDaily.date_local >= days[0].date,
                    FitnessDaily.date_local <= days[-1].date,
                )
            )
        }
        for d in days:
            row = existing.get(d.date)
            if row is None:
                row = FitnessDaily(athlete_id=athlete_id, date_local=d.date)
                session.add(row)
            row.ctl_sim = round(d.ctl, 3)
            row.atl_sim = round(d.atl, 3)
            row.tsb_sim = round(d.tsb, 3)
            row.load_actual = round(d.load, 2)
            row.acwr_7_28 = round(d.acwr_7_28, 4) if d.acwr_7_28 is not None else None
            row.monotony_7 = round(d.monotony_7, 4) if d.monotony_7 is not None else None
            row.strain_7 = round(d.strain_7, 2) if d.strain_7 is not None else None
    as_of = result.report.as_of
    snaps = {
        r.window: r
        for r in session.scalars(
            select(PowerCurveSnapshot).where(
                PowerCurveSnapshot.athlete_id == athlete_id,
                PowerCurveSnapshot.as_of_date == as_of,
                PowerCurveSnapshot.source == "cyp",
            )
        )
    }
    for name, wf in result.ftp.windows.items():
        snap = snaps.get(name)
        if snap is None:
            snap = PowerCurveSnapshot(
                athlete_id=athlete_id, as_of_date=as_of, window=name, source="cyp"
            )
            session.add(snap)
        fit = wf.cp_2p
        snap.durations_s = list(wf.mmp)
        snap.watts = [round(w, 1) for w in wf.mmp.values()]
        snap.w_kg = [round(w / weight_kg, 3) for w in wf.mmp.values()] if weight_kg else None
        snap.cp = round(fit.cp, 1) if fit else None
        snap.w_prime = round(fit.w_prime) if fit else None
        snap.p_max = round(fit.p_max) if fit and fit.p_max else None
        snap.eftp_icu = None


def backfill_durability(
    factory: sessionmaker[Session], store: StreamStore, ds: Dataset, as_of: dt.date
) -> int:
    """Compute ``activity_metrics.durability`` for recent rides analysed before ride-1.1.0.

    Returns how many rows were filled (0 once everything is current, so later runs never touch
    Parquet).
    """
    lo = as_of - dt.timedelta(days=DURABILITY_LOOKBACK_DAYS)
    todo = [
        a
        for a in ds.rides(lo, as_of)
        if a.measured_power and a.has_hr and a.durability is None and (a.kj or 0) >= dur_mod.LATE_KJ
    ]
    if not todo:
        return 0
    filled = 0
    with factory() as s:
        for a in todo:
            ftp = ds.ftp_on(a.date)
            if not ftp:
                continue
            try:
                frame = load_frame(store, a.id)
            except (NotFoundError, AnalysisError):
                continue
            blob = dur_mod.ride_durability_json(frame, ftp)
            row = s.get(ActivityMetrics, a.id)
            if row is not None:
                row.durability = blob or {
                    "version": dur_mod.DURABILITY_VERSION,
                    "ftp": ftp,
                    "ef_by_bucket": {},
                }
                filled += 1
        s.commit()
    return filled


def build_trends(
    factory: sessionmaker[Session],
    store: StreamStore,
    *,
    as_of: dt.date,
    phase: str | None = None,
    history_days: int = HISTORY_DAYS,
    reports_dir: Path | None = None,
    power_fix_until: dt.date | None = None,
    data_quality: DataQuality | None = None,
) -> TrendsReport:
    """Run every longitudinal model as of ``as_of``; persist and return the report.

    Raises:
        AnalysisError: no athlete in the store.
    """
    ds = CACHE.get(factory, power_fix_until=power_fix_until, data_quality=data_quality)
    if ds is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    if backfill_durability(factory, store, ds, as_of):
        ds = CACHE.get(factory, power_fix_until=power_fix_until, data_quality=data_quality)
        assert ds is not None
    result = compute_trends(ds, as_of, phase=phase)
    with factory() as s:
        persist_trends(s, result, weight_kg=ds.weight_kg)
        s.commit()
    if reports_dir is not None:
        write_report(result.report, reports_dir)
    log.info(
        "trends.done",
        as_of=as_of.isoformat(),
        limiters=[lim["id"] for lim in result.report.limiters],
    )
    return result.report


def write_report(report: TrendsReport, reports_dir: Path) -> Path:
    """Write ``trends/{as_of}.json`` and ``trends/latest.json``; returns the dated path."""
    out_dir = Path(reports_dir) / "trends"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_json(), ensure_ascii=False, indent=1)
    dated = out_dir / f"{report.as_of.isoformat()}.json"
    dated.write_text(payload, encoding="utf-8")
    (out_dir / "latest.json").write_text(payload, encoding="utf-8")
    return dated


def load_latest_report(reports_dir: Path) -> dict[str, Any] | None:
    """The last written trends report, if any."""
    p = Path(reports_dir) / "trends" / "latest.json"
    if not p.is_file():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data
