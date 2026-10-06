"""Read-only, column-selected snapshot of everything the models compute from (+ a cache).

Every recompute path (trends, FTP, readiness, planning, API what-ifs) needs the same few
thousand small facts: per-activity load, per-ride metrics, wellness, fitness, settings history
and calendar events. Loading them through full ORM objects drags the raw JSON payloads along
and costs hundreds of milliseconds; this module loads only the needed columns, once, into
frozen dataclasses.

:func:`data_version` is a single aggregate query (counts / max timestamps / sums per input
table). :class:`DatasetCache` keeps the last snapshot per database and reloads only when the
version changes, so repeated recomputes (UI sliders, ``cyp plan`` re-runs) skip I/O entirely.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.data_quality import (
    DataQuality,
    PowerRule,
    infer_meter_gears,
    normalise_serial,
    unreliable_rule,
)
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    Athlete,
    AthleteSettingsHistory,
    FitnessDaily,
    IcuEvent,
    PlannedWorkout,
    PowerCurveSnapshot,
    ReadinessDaily,
    WellnessDaily,
)

DEFAULT_TZ = "Asia/Taipei"


def local_day(start_local: str | None, start_utc: str, tz: str | None) -> dt.date:
    """Local calendar day of an activity (``start_local`` first, else ``start_utc`` in ``tz``)."""
    if start_local:
        try:
            return dt.datetime.fromisoformat(start_local).date()
        except ValueError:
            pass
    from cyp.core.timeutil import local_date, parse_iso

    return local_date(parse_iso(start_utc), tz or DEFAULT_TZ)


@dataclass(frozen=True)
class ActivityRow:
    """One activity with its load and (for analysed rides) the metrics the models use."""

    id: int
    date: dt.date
    sport: str
    name: str | None
    is_ride: bool
    moving_s: int
    kj: float | None
    has_power: bool
    has_hr: bool
    load: float
    load_source: str  # icu | cyp | none
    icu_zone_times: list[float] | None = None
    feel: int | None = None
    rpe: int | None = None
    # --- activity_metrics (None when not analysed)
    analysed: bool = False
    tss: float | None = None
    tss_source: str | None = None
    np_w: float | None = None
    if_: float | None = None
    ef: float | None = None
    power_curve: Mapping[int, float] = field(default_factory=dict)
    tiz_power: Mapping[str, float] | None = None
    tiz_hr: Mapping[str, float] | None = None
    climbs: tuple[dict[str, Any], ...] = ()
    decoupling_pct: float | None = None
    decoupling_reliable: bool = False
    hr_lag_s: float | None = None
    status: str | None = None
    classification: str | None = None
    durability: Mapping[str, Any] | None = None
    # --- data quality (docs/04 §7)
    power_meter_serial: str | None = None
    gear_id: str | None = None
    #: Set by :meth:`Dataset.with_data_quality` when a ``power_unreliable`` rule matches.
    power_unreliable: PowerRule | None = None

    @property
    def measured_power(self) -> bool:
        """Analysed with a real power meter (not HR / estimated)."""
        return self.tss_source == "power"

    @property
    def power_ok(self) -> bool:
        """Measured power that may feed power models (curves, CP, FTP, climbs, limiters)."""
        return self.measured_power and self.power_unreliable is None


@dataclass(frozen=True)
class WellnessRow:
    """Readiness-relevant wellness fields of one day."""

    date: dt.date
    ctl: float | None = None
    atl: float | None = None
    hrv: float | None = None
    resting_hr: float | None = None
    sleep_s: float | None = None
    sleep_score: float | None = None
    soreness: float | None = None
    fatigue: float | None = None
    stress: float | None = None
    mood: float | None = None
    injury: float | None = None
    weight_kg: float | None = None
    eftp: float | None = None
    ctl_load: float | None = None  # icu's load for the day as used in its CTL


@dataclass(frozen=True)
class FitnessRow:
    """``fitness_daily`` of one day (icu ledger + our simulation)."""

    date: dt.date
    ctl_icu: float | None
    atl_icu: float | None
    tsb_icu: float | None
    ctl_sim: float | None
    atl_sim: float | None
    tsb_sim: float | None
    acwr: float | None
    monotony: float | None

    @property
    def ctl(self) -> float | None:
        """Icu when present, else simulated."""
        return self.ctl_icu if self.ctl_icu is not None else self.ctl_sim

    @property
    def atl(self) -> float | None:
        """Icu when present, else simulated."""
        return self.atl_icu if self.atl_icu is not None else self.atl_sim

    @property
    def tsb(self) -> float | None:
        """Icu when present, else simulated."""
        return self.tsb_icu if self.tsb_icu is not None else self.tsb_sim


@dataclass(frozen=True)
class SettingsRow:
    """One ``athlete_settings_history`` row."""

    effective_from: dt.date
    ftp: float | None
    lthr: float | None
    max_hr: float | None
    resting_hr: float | None
    weight_kg: float | None
    source: str


@dataclass(frozen=True)
class EventRow:
    """One calendar event (ours or the athlete's)."""

    id: int
    date: dt.date | None
    end_date: dt.date | None
    category: str
    name: str | None
    external_id: str | None
    load: float | None
    training_availability: str | None
    max_training_time: int | None

    @property
    def ours(self) -> bool:
        """Created by cyp (``cyp:`` external_id)."""
        return (self.external_id or "").startswith("cyp:")


@dataclass(frozen=True)
class PlannedRow:
    """One live (not cancelled / superseded) planned workout of ours."""

    date: dt.date
    external_id: str
    template_id: str | None
    params: Mapping[str, int]
    role: str
    indoor: bool
    target_tss: float | None
    status: str


@dataclass(frozen=True)
class ReadinessRow:
    """Stored readiness verdict."""

    date: dt.date
    score: float | None
    status: str | None
    recommendation: str | None


@dataclass(frozen=True)
class Dataset:
    """The snapshot. Lists are sorted by date; dicts are keyed by date."""

    version: str
    athlete_id: int
    weight_kg: float | None
    settings: tuple[SettingsRow, ...]
    activities: tuple[ActivityRow, ...]
    loads: Mapping[dt.date, float]
    wellness: Mapping[dt.date, WellnessRow]
    fitness: Mapping[dt.date, FitnessRow]
    readiness: Mapping[dt.date, ReadinessRow]
    events: tuple[EventRow, ...]
    icu_models: Mapping[str, dict[str, float | None]]  # window -> {cp, w_prime, p_max, eftp}
    planned: Mapping[dt.date, tuple[PlannedRow, ...]] = field(default_factory=dict)
    #: Set by :meth:`with_power_fix`: loads up to this day come from our re-analysis.
    power_fix_until: dt.date | None = None
    #: Set by :meth:`with_data_quality`.
    data_quality: DataQuality | None = None

    def with_data_quality(self, dq: DataQuality | None) -> Dataset:
        """Copy with the load fix (``dq.load_fix_until``) and ``power_unreliable`` flags applied.

        Flags use :func:`cyp.core.data_quality.unreliable_rule` on every ride with power; the
        same function flags ``activity_metrics.comparison`` at analysis time.
        """
        if dq is None or dq.empty:
            return self
        ds = self.with_power_fix(dq.load_fix_until)
        if dq.power_rules:
            gears = infer_meter_gears(
                ((a.date, a.power_meter_serial, a.gear_id) for a in ds.activities if a.is_ride),
                dq.power_rules,
            )
            acts = []
            for a in ds.activities:
                rule = (
                    unreliable_rule(dq.power_rules, a.date, a.power_meter_serial, a.gear_id, gears)
                    if a.is_ride and a.has_power
                    else None
                )
                acts.append(replace(a, power_unreliable=rule) if rule is not None else a)
            ds = replace(ds, activities=tuple(acts))
        return replace(ds, data_quality=dq)

    @property
    def power_unreliable_rides(self) -> list[ActivityRow]:
        """Rides flagged ``power_unreliable``."""
        return [a for a in self.activities if a.power_unreliable is not None]

    def with_power_fix(self, until: dt.date | None) -> Dataset:
        """Copy whose daily loads up to ``until`` use our TSS instead of icu's ledger.

        For a day ≤ ``until`` the load is Σ per activity of: our TSS for rides analysed with
        measured power (empty power counted as 0 W), else the activity's load. icu's numbers for
        those days were computed from files recorded with "include zeros" off and run high.
        """
        if until is None:
            return self
        loads = dict(self.loads)
        per_day: dict[dt.date, float] = {}
        for a in self.activities:
            if a.date > until:
                continue
            own = a.tss if (a.is_ride and a.measured_power and a.tss is not None) else None
            per_day[a.date] = per_day.get(a.date, 0.0) + (own if own is not None else a.load)
        for d in [d for d in loads if d <= until]:
            loads[d] = per_day.get(d, 0.0)
        loads.update(per_day)
        return replace(self, loads=loads, power_fix_until=until)

    @property
    def planned_load(self) -> dict[dt.date, float]:
        """Σ target TSS of our live planned workouts per day."""
        return {
            d: sum(p.target_tss or 0.0 for p in rows)
            for d, rows in self.planned.items()
            if any(p.target_tss is not None for p in rows)
        }

    # ------------------------------------------------------------------ helpers

    def rides(
        self, start: dt.date | None = None, end: dt.date | None = None
    ) -> Iterator[ActivityRow]:
        """Analysed rides with ``start <= date <= end``."""
        for a in self.activities:
            if not (a.is_ride and a.analysed):
                continue
            if (start is None or a.date >= start) and (end is None or a.date <= end):
                yield a

    def on(self, day: dt.date) -> list[ActivityRow]:
        """Every activity of a local day."""
        return [a for a in self.activities if a.date == day]

    def settings_on(self, day: dt.date) -> SettingsRow | None:
        """Latest settings row effective on ``day`` (else the earliest)."""
        best = None
        for s in self.settings:
            if s.effective_from <= day:
                best = s
        return best or (self.settings[0] if self.settings else None)

    def ftp_on(self, day: dt.date) -> float | None:
        """FTP effective on ``day``."""
        for s in reversed(self.settings):
            if s.effective_from <= day and s.ftp:
                return float(s.ftp)
        latest = next((s for s in reversed(self.settings) if s.ftp), None)
        return float(latest.ftp) if latest and latest.ftp else None

    def ctl_atl(self, day: dt.date) -> tuple[float, float]:
        """CTL/ATL on ``day`` (fitness, else wellness, else the last earlier value, else 0)."""
        f = self.fitness.get(day)
        if f is not None and f.ctl is not None:
            return float(f.ctl), float(f.atl or f.ctl)
        w = self.wellness.get(day)
        if w is not None and w.ctl is not None:
            return float(w.ctl), float(w.atl or w.ctl)
        # Latest earlier value from either source (fitness rows only exist after trends ran).
        best: tuple[dt.date, float, float] | None = None
        for d, f in self.fitness.items():
            if d < day and f.ctl is not None and (best is None or d > best[0]):
                best = (d, float(f.ctl), float(f.atl or f.ctl))
        for d, w in self.wellness.items():
            if d < day and w.ctl is not None and (best is None or d > best[0]):
                best = (d, float(w.ctl), float(w.atl or w.ctl))
        if best is not None:
            return best[1], best[2]
        return 0.0, 0.0

    def events_between(self, start: dt.date, end: dt.date) -> list[EventRow]:
        """Events starting in ``[start, end]``."""
        return [e for e in self.events if e.date is not None and start <= e.date <= end]


# ------------------------------------------------------------------------------- version


def data_version(session: Session) -> str:
    """Cheap fingerprint of every input table (one round trip of scalar aggregates)."""
    aggregates: list[Any] = [
        func.count(Activity.id),
        func.max(Activity.id),
        func.max(Activity.updated_at),
        func.max(Activity.fetched_at),
        func.sum(Activity.icu_training_load),
        func.count(ActivityMetrics.activity_id),
        func.max(ActivityMetrics.computed_at),
        func.count(WellnessDaily.date_local),
        func.max(WellnessDaily.fetched_at),
        func.count(AthleteSettingsHistory.id),
        func.max(AthleteSettingsHistory.id),
        func.count(IcuEvent.id),
        func.max(IcuEvent.fetched_at),
        func.count(FitnessDaily.date_local),
        func.sum(FitnessDaily.ctl_sim),
        func.sum(FitnessDaily.ctl_icu),
        func.count(ReadinessDaily.date_local),
        func.sum(ReadinessDaily.score_0_100),
        func.count(PowerCurveSnapshot.id),
        func.max(PowerCurveSnapshot.id),
        func.count(PlannedWorkout.id),
        func.sum(PlannedWorkout.revision),
        func.sum(PlannedWorkout.target_tss),
    ]
    stmt = select(*[select(agg).scalar_subquery() for agg in aggregates])
    return "|".join("" if v is None else str(v) for v in session.execute(stmt).one())


# --------------------------------------------------------------------------------- load


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _curve(blob: Any) -> dict[int, float]:
    if not isinstance(blob, dict):
        return {}
    inner = blob.get("watts")
    watts: dict[Any, Any] = inner if isinstance(inner, dict) else blob
    out: dict[int, float] = {}
    for k, v in watts.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def load_dataset(session: Session, *, version: str | None = None) -> Dataset | None:
    """Snapshot of the primary athlete (``None`` when the store has no athlete)."""
    athlete = session.execute(
        select(Athlete.id, Athlete.weight_kg).order_by(Athlete.intervals_id.is_(None), Athlete.id)
    ).first()
    if athlete is None:
        return None
    athlete_id, weight = athlete
    version = version or data_version(session)

    settings = tuple(
        SettingsRow(
            r.effective_from,
            r.ftp,
            _f(r.lthr),
            _f(r.max_hr),
            _f(r.resting_hr),
            r.weight_kg,
            r.source,
        )
        for r in session.execute(
            select(
                AthleteSettingsHistory.effective_from,
                AthleteSettingsHistory.ftp,
                AthleteSettingsHistory.lthr,
                AthleteSettingsHistory.max_hr,
                AthleteSettingsHistory.resting_hr,
                AthleteSettingsHistory.weight_kg,
                AthleteSettingsHistory.source,
            )
            .where(AthleteSettingsHistory.athlete_id == athlete_id)
            .order_by(AthleteSettingsHistory.effective_from, AthleteSettingsHistory.id)
        )
    )

    rows = session.execute(
        select(
            Activity.id,
            Activity.start_local,
            Activity.start_utc,
            Activity.tz,
            Activity.sport_type,
            Activity.name,
            Activity.is_ride,
            Activity.moving_s,
            Activity.kj,
            Activity.has_power,
            Activity.has_hr,
            Activity.icu_training_load,
            Activity.icu_zone_times,
            Activity.feel,
            Activity.icu_rpe,
            Activity.gear_id,
            Activity.raw_intervals_json["power_meter_serial"].as_string().label("pm_serial"),
            ActivityMetrics.activity_id,
            ActivityMetrics.tss,
            ActivityMetrics.tss_source,
            ActivityMetrics.np_w,
            ActivityMetrics.if_,
            ActivityMetrics.ef,
            ActivityMetrics.power_curve,
            ActivityMetrics.time_in_zone_power,
            ActivityMetrics.time_in_zone_hr,
            ActivityMetrics.climbs,
            ActivityMetrics.decoupling_pct,
            ActivityMetrics.hr_drift_detail,
            ActivityMetrics.hr_lag_s,
            ActivityMetrics.status,
            ActivityMetrics.pacing,
            ActivityMetrics.durability,
        ).outerjoin(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
    ).all()
    acts: list[ActivityRow] = []
    loads: dict[dt.date, float] = {}
    for r in rows:
        day = local_day(r.start_local, r.start_utc, r.tz)
        if r.icu_training_load is not None:
            load, src = float(r.icu_training_load), "icu"
        elif r.tss is not None:
            load, src = float(r.tss), "cyp"
        else:
            load, src = 0.0, "none"
        loads[day] = loads.get(day, 0.0) + load
        detail = r.hr_drift_detail if isinstance(r.hr_drift_detail, dict) else {}
        pacing = r.pacing if isinstance(r.pacing, dict) else {}
        acts.append(
            ActivityRow(
                id=r.id,
                date=day,
                sport=r.sport_type,
                name=r.name,
                is_ride=bool(r.is_ride),
                moving_s=int(r.moving_s or 0),
                kj=r.kj,
                has_power=bool(r.has_power),
                has_hr=bool(r.has_hr),
                load=load,
                load_source=src,
                icu_zone_times=r.icu_zone_times if isinstance(r.icu_zone_times, list) else None,
                feel=r.feel,
                rpe=r.icu_rpe,
                analysed=r.activity_id is not None,
                tss=r.tss,
                tss_source=r.tss_source,
                np_w=r.np_w,
                if_=r.if_,
                ef=r.ef,
                power_curve=_curve(r.power_curve),
                tiz_power=r.time_in_zone_power if isinstance(r.time_in_zone_power, dict) else None,
                tiz_hr=r.time_in_zone_hr if isinstance(r.time_in_zone_hr, dict) else None,
                climbs=tuple(c for c in (r.climbs or []) if isinstance(c, dict)),
                decoupling_pct=r.decoupling_pct,
                decoupling_reliable=bool(detail.get("reliable")),
                hr_lag_s=r.hr_lag_s,
                status=r.status,
                classification=pacing.get("classification")
                if isinstance(pacing.get("classification"), str)
                else None,
                durability=r.durability if isinstance(r.durability, dict) else None,
                power_meter_serial=normalise_serial(r.pm_serial),
                gear_id=r.gear_id,
            )
        )
    acts.sort(key=lambda a: (a.date, a.id))

    wellness: dict[dt.date, WellnessRow] = {}
    for w in session.execute(
        select(
            WellnessDaily.date_local,
            WellnessDaily.ctl,
            WellnessDaily.atl,
            WellnessDaily.hrv,
            WellnessDaily.resting_hr,
            WellnessDaily.sleep_s,
            WellnessDaily.sleep_score,
            WellnessDaily.soreness,
            WellnessDaily.fatigue,
            WellnessDaily.stress,
            WellnessDaily.mood,
            WellnessDaily.injury,
            WellnessDaily.weight_kg,
            WellnessDaily.raw_json,
            WellnessDaily.ctl_load,
        ).where(WellnessDaily.athlete_id == athlete_id)
    ):
        eftp = None
        info = w.raw_json.get("sportInfo") if isinstance(w.raw_json, dict) else None
        for s in info if isinstance(info, list) else []:
            if isinstance(s, dict) and s.get("type") == "Ride":
                eftp = _f(s.get("eftp"))
        wellness[w.date_local] = WellnessRow(
            w.date_local,
            w.ctl,
            w.atl,
            w.hrv,
            w.resting_hr,
            _f(w.sleep_s),
            w.sleep_score,
            _f(w.soreness),
            _f(w.fatigue),
            _f(w.stress),
            _f(w.mood),
            _f(w.injury),
            w.weight_kg,
            eftp,
            w.ctl_load,
        )

    # icu is the load ledger (ADR-0003): where wellness carries icu's own daily ctlLoad, the
    # day's load is that number. It excludes what icu does not count towards fitness (e.g.
    # strength / yoga per the athlete's icu settings, rides icu has no load for). Days without
    # it (e.g. Strava-only history) keep the activity sum.
    for day, wrow in wellness.items():
        if wrow.ctl_load is not None:
            loads[day] = float(wrow.ctl_load)

    fitness = {
        r.date_local: FitnessRow(
            r.date_local,
            r.ctl_icu,
            r.atl_icu,
            r.tsb_icu,
            r.ctl_sim,
            r.atl_sim,
            r.tsb_sim,
            r.acwr_7_28,
            r.monotony_7,
        )
        for r in session.execute(
            select(
                FitnessDaily.date_local,
                FitnessDaily.ctl_icu,
                FitnessDaily.atl_icu,
                FitnessDaily.tsb_icu,
                FitnessDaily.ctl_sim,
                FitnessDaily.atl_sim,
                FitnessDaily.tsb_sim,
                FitnessDaily.acwr_7_28,
                FitnessDaily.monotony_7,
            ).where(FitnessDaily.athlete_id == athlete_id)
        )
    }
    readiness = {
        r.date_local: ReadinessRow(r.date_local, r.score_0_100, r.status, r.recommendation)
        for r in session.execute(
            select(
                ReadinessDaily.date_local,
                ReadinessDaily.score_0_100,
                ReadinessDaily.status,
                ReadinessDaily.recommendation,
            ).where(ReadinessDaily.athlete_id == athlete_id)
        )
    }

    def _day(v: str | None) -> dt.date | None:
        try:
            return dt.date.fromisoformat((v or "")[:10])
        except ValueError:
            return None

    events = tuple(
        sorted(
            (
                EventRow(
                    e.id,
                    _day(e.start_date_local),
                    _day(e.end_date_local),
                    e.category or "",
                    e.name,
                    e.external_id,
                    e.icu_training_load,
                    e.training_availability,
                    e.max_training_time,
                )
                for e in session.execute(
                    select(
                        IcuEvent.id,
                        IcuEvent.start_date_local,
                        IcuEvent.end_date_local,
                        IcuEvent.category,
                        IcuEvent.name,
                        IcuEvent.external_id,
                        IcuEvent.icu_training_load,
                        IcuEvent.training_availability,
                        IcuEvent.max_training_time,
                    )
                )
            ),
            key=lambda e: (e.date or dt.date.min, e.id),
        )
    )

    icu_models: dict[str, dict[str, float | None]] = {}
    for snap in session.execute(
        select(
            PowerCurveSnapshot.window,
            PowerCurveSnapshot.cp,
            PowerCurveSnapshot.w_prime,
            PowerCurveSnapshot.p_max,
            PowerCurveSnapshot.eftp_icu,
        )
        .where(PowerCurveSnapshot.athlete_id == athlete_id, PowerCurveSnapshot.source == "icu")
        .order_by(PowerCurveSnapshot.as_of_date)
    ):
        icu_models[snap.window] = {
            "cp": snap.cp,
            "w_prime": snap.w_prime,
            "p_max": snap.p_max,
            "eftp": snap.eftp_icu,
        }

    planned: dict[dt.date, list[PlannedRow]] = {}
    for p in session.execute(
        select(
            PlannedWorkout.date_local,
            PlannedWorkout.external_id,
            PlannedWorkout.template_id,
            PlannedWorkout.steps,
            PlannedWorkout.indoor,
            PlannedWorkout.target_tss,
            PlannedWorkout.status,
        )
        .where(
            PlannedWorkout.athlete_id == athlete_id,
            PlannedWorkout.status.not_in(("cancelled", "superseded")),
        )
        .order_by(PlannedWorkout.date_local, PlannedWorkout.slot)
    ):
        steps = p.steps if isinstance(p.steps, dict) else {}
        planned.setdefault(p.date_local, []).append(
            PlannedRow(
                p.date_local,
                p.external_id,
                p.template_id,
                dict(steps.get("params") or {}),
                str(steps.get("role") or "hit"),
                bool(p.indoor),
                p.target_tss,
                p.status,
            )
        )

    return Dataset(
        planned={d: tuple(v) for d, v in planned.items()},
        version=version,
        athlete_id=athlete_id,
        weight_kg=weight,
        settings=settings,
        activities=tuple(acts),
        loads=loads,
        wellness=wellness,
        fitness=fitness,
        readiness=readiness,
        events=events,
        icu_models=icu_models,
    )


class DatasetCache:
    """Last :class:`Dataset` per database URL, reloaded when :func:`data_version` changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Dataset] = {}
        self._fixed: dict[tuple[str, DataQuality], Dataset] = {}
        self.loads = 0  # how many times a snapshot was (re)loaded — for tests / metrics

    def get(
        self,
        factory: sessionmaker[Session],
        *,
        power_fix_until: dt.date | None = None,
        data_quality: DataQuality | None = None,
    ) -> Dataset | None:
        """Current snapshot (one version query when nothing changed).

        ``data_quality`` returns the :meth:`Dataset.with_data_quality` view (memoised);
        ``power_fix_until`` alone is shorthand for a load fix without power rules.
        """
        ds = self._get(factory)
        dq = data_quality
        if dq is None and power_fix_until is not None:
            dq = DataQuality(load_fix_until=power_fix_until)
        if ds is None or dq is None or dq.empty:
            return ds
        key = (ds.version, dq)
        with self._lock:
            hit = self._fixed.get(key)
        if hit is None:
            hit = ds.with_data_quality(dq)
            with self._lock:
                self._fixed = {key: hit}
        return hit

    def _get(self, factory: sessionmaker[Session]) -> Dataset | None:
        bind = factory.kw.get("bind")
        key = str(bind.url) if bind is not None else "default"
        with factory() as s:
            version = data_version(s)
            with self._lock:
                cached = self._entries.get(key)
                if cached is not None and cached.version == version:
                    return cached
            ds = load_dataset(s, version=version)
        with self._lock:
            self.loads += 1
            if ds is not None:
                self._entries[key] = ds
            else:
                self._entries.pop(key, None)
        return ds

    def clear(self) -> None:
        """Drop every cached snapshot."""
        with self._lock:
            self._entries.clear()
            self._fixed.clear()


#: Process-wide cache used by the services (CLI and API share it within one process).
CACHE = DatasetCache()
