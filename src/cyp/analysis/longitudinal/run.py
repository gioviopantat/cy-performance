"""Longitudinal job: read the store, run every trend model, persist, return a report.

``build_trends`` is the only function here touching the DB/Parquet; the models in sibling
modules are pure. Persisted:

- ``fitness_daily``: ``ctl_sim/atl_sim/tsb_sim``, ``load_actual``, ``acwr_7_28``,
  ``monotony_7``, ``strain_7`` (icu columns untouched).
- ``power_curve_snapshots`` (``source='cyp'``, windows ``42d``/``90d``): our MMP + CP fit.
- ``{reports_dir}/trends/{as_of}.json`` and ``latest.json``: the full :class:`TrendsReport`
  including every Explanation (``cyp explain`` reads it).

Daily load: Σ over the day's activities (all sports — strength and yoga count, docs/05) of
icu ``icu_training_load``, else our ``activity_metrics.tss``, else 0.
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
from cyp.analysis.longitudinal import pdc, pmc
from cyp.analysis.longitudinal import tid as tid_mod
from cyp.analysis.ride.frames import load_frame
from cyp.analysis.run import activity_local_date
from cyp.core.errors import AnalysisError, NotFoundError
from cyp.core.explain import Explanation
from cyp.logging import get_logger
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    Athlete,
    FitnessDaily,
    PowerCurveSnapshot,
    WellnessDaily,
)
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.streams import StreamStore

log = get_logger(__name__)

TRENDS_VERSION = "trends_v1"
DURABILITY_LOOKBACK_DAYS = 168
TID_WEEKS = 12


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
    """``{date: total load}`` plus the per-activity breakdown (see module docstring)."""
    rows = session.execute(
        select(Activity, ActivityMetrics.tss).outerjoin(
            ActivityMetrics, ActivityMetrics.activity_id == Activity.id
        )
    ).all()
    totals: dict[dt.date, float] = {}
    detail: dict[dt.date, list[DayLoad]] = {}
    for act, tss in rows:
        if act.icu_training_load is not None:
            load, src = float(act.icu_training_load), "icu"
        elif tss is not None:
            load, src = float(tss), "cyp"
        else:
            load, src = 0.0, "none"
        d = activity_local_date(act)
        totals[d] = totals.get(d, 0.0) + load
        detail.setdefault(d, []).append(DayLoad(act.id, load, src))
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


def _current_ftp(session: Session, athlete_id: int, as_of: dt.date) -> float | None:
    repo = AthleteSettingsRepo(session)
    for row in (repo.effective_on(athlete_id, as_of), repo.latest(athlete_id)):
        if row is not None and row.ftp:
            return float(row.ftp)
    return None


def _icu_eftp_series(wellness: list[WellnessDaily]) -> dict[dt.date, float]:
    out: dict[dt.date, float] = {}
    for w in wellness:
        info = (w.raw_json or {}).get("sportInfo") if isinstance(w.raw_json, dict) else None
        if not isinstance(info, list):
            continue
        for s in info:
            if (
                isinstance(s, dict)
                and s.get("type") == "Ride"
                and isinstance(s.get("eftp"), int | float)
            ):
                out[w.date_local] = float(s["eftp"])
    return out


def _upsert_fitness(session: Session, athlete_id: int, d: pmc.PMCDay) -> None:
    row = session.get(FitnessDaily, (athlete_id, d.date))
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


def _upsert_cyp_curve(
    session: Session,
    athlete_id: int,
    as_of: dt.date,
    window: str,
    mmp: dict[int, float],
    fit: pdc.CPFit | None,
    weight: float | None,
) -> None:
    stmt = select(PowerCurveSnapshot).where(
        PowerCurveSnapshot.athlete_id == athlete_id,
        PowerCurveSnapshot.as_of_date == as_of,
        PowerCurveSnapshot.window == window,
        PowerCurveSnapshot.source == "cyp",
    )
    row = session.scalar(stmt)
    if row is None:
        row = PowerCurveSnapshot(
            athlete_id=athlete_id, as_of_date=as_of, window=window, source="cyp"
        )
        session.add(row)
    row.durations_s = list(mmp)
    row.watts = [round(w, 1) for w in mmp.values()]
    row.w_kg = [round(w / weight, 3) for w in mmp.values()] if weight else None
    row.cp = round(fit.cp, 1) if fit else None
    row.w_prime = round(fit.w_prime) if fit else None
    row.p_max = round(fit.p_max) if fit and fit.p_max else None
    row.eftp_icu = None


def _icu_snapshot(session: Session, athlete_id: int, window: str) -> PowerCurveSnapshot | None:
    stmt = (
        select(PowerCurveSnapshot)
        .where(
            PowerCurveSnapshot.athlete_id == athlete_id,
            PowerCurveSnapshot.window == window,
            PowerCurveSnapshot.source == "icu",
        )
        .order_by(PowerCurveSnapshot.as_of_date.desc())
    )
    return session.scalars(stmt).first()


def build_trends(
    factory: sessionmaker[Session],
    store: StreamStore,
    *,
    as_of: dt.date,
    phase: str | None = None,
    history_days: int = 400,
    reports_dir: Path | None = None,
) -> TrendsReport:
    """Run every longitudinal model as of ``as_of``; persist and return the report.

    Raises:
        AnalysisError: no athlete in the store.
    """
    with factory() as s:
        athlete_id = primary_athlete_id(s)
        if athlete_id is None:
            raise AnalysisError("no athlete in the store; run `cyp sync` first")
        athlete = s.get(Athlete, athlete_id)
        weight = athlete.weight_kg if athlete else None
        ftp = _current_ftp(s, athlete_id, as_of)
        report = TrendsReport(as_of=as_of, athlete_id=athlete_id, ftp=ftp)
        start = as_of - dt.timedelta(days=history_days)
        loads, _ = daily_loads(s)
        wellness = list(
            s.scalars(
                select(WellnessDaily)
                .where(WellnessDaily.athlete_id == athlete_id, WellnessDaily.date_local <= as_of)
                .order_by(WellnessDaily.date_local)
            )
        )

        # ---- PMC replay, seeded from the earliest icu value in the window
        icu = {w.date_local: (w.ctl, w.atl) for w in wellness if w.ctl is not None}
        seed_day = min((d for d in icu if d >= start), default=None)
        if seed_day is not None:
            c0, a0 = icu[seed_day]
            seed = pmc.PMCState(seed_day, float(c0 or 0), float(a0 or 0))
            series, agree = pmc.best_params(seed, loads, as_of, icu)
            for day_state in series:
                _upsert_fitness(s, athlete_id, day_state)
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

        # ---- rides with metrics
        ride_rows = s.execute(
            select(Activity, ActivityMetrics)
            .join(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
            .where(Activity.is_ride.is_(True))
        ).all()
        rides = [(a, m, activity_local_date(a)) for a, m in ride_rows]
        rides = [(a, m, d) for a, m, d in rides if d <= as_of]

        # ---- power-duration
        mmp_by_window: dict[str, dict[int, float]] = {}
        for window, n_days in (("42d", 42), ("90d", 90)):
            lo = as_of - dt.timedelta(days=n_days - 1)
            mmp = pdc.mmp_from_rides(
                m.power_curve
                for a, m, d in rides
                if d >= lo and m.tss_source == "power" and isinstance(m.power_curve, dict)
            )
            mmp_by_window[window] = mmp
            fit2, fit3 = pdc.fit_cp_2p(mmp), pdc.fit_cp_3p(mmp)
            icu_snap = _icu_snapshot(s, athlete_id, window)
            icu_model = (
                {
                    "criticalPower": icu_snap.cp,
                    "wPrime": icu_snap.w_prime,
                    "pMax": icu_snap.p_max,
                    "ftp": icu_snap.eftp_icu,
                }
                if icu_snap
                else None
            )
            entry: dict[str, Any] = {
                "mmp": {str(k): round(v, 1) for k, v in mmp.items()},
                "icu": icu_model,
            }
            for fit in (fit2, fit3):
                if fit is None:
                    continue
                cmp = pdc.compare_with_icu(fit, icu_model)
                entry[fit.model] = {
                    "cp": round(fit.cp, 1),
                    "w_prime": round(fit.w_prime),
                    "p_max": round(fit.p_max) if fit.p_max else None,
                    "r2": round(fit.r2, 4) if fit.r2 is not None else None,
                    "eftp": round(fit.eftp, 1),
                    "cp_diff_vs_icu_pct": round(cmp.cp_diff_pct, 2)
                    if cmp.cp_diff_pct is not None
                    else None,
                }
                if window == "42d":
                    report.add(pdc.explain_fit(fit, cmp, as_of=as_of))
            report.cp_fits[window] = entry
            _upsert_cyp_curve(s, athlete_id, as_of, window, mmp, fit2, weight)

        # ---- FTP proposal (never applied)
        if ftp:
            estimates = _icu_eftp_series(wellness)
            sources = ["icu_eftp"]
            if not estimates:
                sources = ["cyp_cp_2p_42d"]
                for i in range(56):
                    day = as_of - dt.timedelta(days=i)
                    lo = day - dt.timedelta(days=41)
                    fit = pdc.fit_cp_2p(
                        pdc.mmp_from_rides(
                            m.power_curve
                            for a, m, d in rides
                            if lo <= d <= day and m.tss_source == "power"
                        )
                    )
                    if fit is not None:
                        estimates[day] = fit.eftp
            series_e = pdc.daily_series(estimates, as_of - dt.timedelta(days=55), as_of)
            prop = pdc.ftp_proposal(
                ftp,
                series_e,
                as_of=as_of,
                sources=sources,
                best_20min_w=mmp_by_window.get("42d", {}).get(1200),
            )
            report.ftp_proposal = {
                "current_ftp": prop.current_ftp,
                "proposed_ftp": prop.proposed_ftp,
                "direction": prop.direction,
                "days_sustained": prop.days_sustained,
                "median_estimate": prop.median_estimate,
                "change_pct": prop.change_pct,
                "best_20min_w": prop.best_20min_w,
                "unsupported": prop.unsupported,
                "sources": prop.sources,
            }
            report.add(prop.explanation)

        # ---- durability (reads Parquet)
        dur: list[dur_mod.RideDurability] = []
        if ftp:
            lo = as_of - dt.timedelta(days=DURABILITY_LOOKBACK_DAYS)
            for a, m, d in rides:
                if d < lo or not a.has_power or not a.has_hr or m.tss_source != "power":
                    continue
                if (a.kj or 0) < dur_mod.LATE_KJ:
                    continue
                try:
                    frame = load_frame(store, a.id)
                except (NotFoundError, AnalysisError):
                    continue
                r = dur_mod.ef_by_kj(frame, ftp, activity_id=a.id, date=d)
                if r is not None:
                    dur.append(r)
        blocks = dur_mod.durability_trend(dur, end=as_of)
        report.durability_blocks = [asdict(b) for b in blocks]
        report.add(dur_mod.explain_durability(blocks, as_of=as_of))

        # ---- TID
        lo = tid_mod.week_start(as_of) - dt.timedelta(weeks=TID_WEEKS - 1)
        tiz_rides = []
        for a, m, d in rides:
            if d < lo:
                continue
            picked = tid_mod.tiz_from_metrics(
                m.time_in_zone_power, m.time_in_zone_hr, a.icu_zone_times
            )
            if picked:
                tiz_rides.append(tid_mod.RideTiz(d, picked[0], picked[1]))
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
        recent = [
            w for w in weeks if w.week_start >= tid_mod.week_start(as_of) - dt.timedelta(weeks=3)
        ]
        tot = sum(w.total_s for w in recent)
        mid_share = sum(w.mid_s for w in recent) / tot if tot > 0 else None

        # ---- repeat climbs
        boards = climbs_mod.leaderboard((a.id, d, m.climbs) for a, m, d in rides)
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
                ftp=ftp or 0.0,
                mmp=mmp_by_window.get("42d", {}),
                durability_ratio=last_ratio,
                mid_share_4w=mid_share,
                ctl=report.pmc_today["ctl"] if report.pmc_today else None,
            )
            if ftp
            else []
        )
        for lim in limiters:
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
        s.commit()

    if reports_dir is not None:
        write_report(report, reports_dir)
    log.info(
        "trends.done", as_of=as_of.isoformat(), limiters=[lim["id"] for lim in report.limiters]
    )
    return report


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
