"""Collect the structured facts a daily / weekly report renders (docs/04 §6, docs/07 §3).

Builders here only *read* persisted results — ``activity_metrics``, ``readiness_daily``,
``fitness_daily``, ``planned_workouts``, ``icu_events`` and the trends report JSON — and turn
them into plain dicts. The one exception is the weekly TID of a past week, which is computed
on the fly with the same pure function the trends job uses and returned with its Explanation.

Every fact that carries a verdict also carries its Explanation dict so the template can show
*why*; the facts dict is written next to the Markdown (``.json``) for the future LLM narrator.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cyp.analysis.longitudinal import tid as tid_mod
from cyp.analysis.longitudinal.run import load_latest_report, primary_athlete_id
from cyp.analysis.ride.classify import CLASS_ZH
from cyp.analysis.run import activity_local_date
from cyp.core.errors import AnalysisError
from cyp.core.timeutil import week_start
from cyp.settings import AthleteConfig
from cyp.store.models import (
    Activity,
    ActivityMetrics,
    FitnessDaily,
    IcuEvent,
    PlannedWorkout,
    ReadinessDaily,
)
from cyp.store.repo.athlete_settings import AthleteSettingsRepo

#: Warning thresholds quoted in the report (docs/glossary/ramp_rate_acwr_monotony.md).
ACWR_HIGH = 1.3
ACWR_LOW = 0.8
MONOTONY_HIGH = 2.0
DEFAULT_RAMP_CAP = 6.0
DEFAULT_TSB_FLOOR = -30.0


def _rides_on(session: Session, day: dt.date) -> list[tuple[Activity, ActivityMetrics | None]]:
    lo = (day - dt.timedelta(days=1)).isoformat()
    hi = (day + dt.timedelta(days=2)).isoformat()
    rows = session.execute(
        select(Activity, ActivityMetrics)
        .outerjoin(ActivityMetrics, ActivityMetrics.activity_id == Activity.id)
        .where(Activity.start_utc >= lo, Activity.start_utc < hi)
        .order_by(Activity.start_utc)
    ).all()
    return [(a, m) for a, m in rows if activity_local_date(a) == day]


def ride_fact(a: Activity, m: ActivityMetrics | None) -> dict[str, Any]:
    """One activity as report facts (non-rides keep only name / load)."""
    out: dict[str, Any] = {
        "activity_id": a.id,
        "name": a.name or a.sport_type,
        "sport": a.sport_type,
        "is_ride": a.is_ride,
        "moving_min": round((a.moving_s or 0) / 60),
        "distance_km": round(a.distance_m / 1000, 1) if a.distance_m else None,
        "elev_gain_m": round(a.elev_gain_m) if a.elev_gain_m else None,
        "load": a.icu_training_load,
        "load_source": "icu" if a.icu_training_load is not None else None,
        "feel": a.feel,
        "rpe": a.icu_rpe,
        # Ride-only keys default to None so templates can test them uniformly.
        "np_w": None,
        "if": None,
        "tss": None,
        "tss_source": None,
        "ef": None,
        "decoupling_pct": None,
        "hr_lag_s": None,
        "classification": None,
        "classification_zh": None,
        "status": None,
        "next": None,
        "climbs": 0,
        "explanation": None,
    }
    if m is not None:
        cls = (m.pacing or {}).get("classification") if isinstance(m.pacing, dict) else None
        out.update(
            {
                "np_w": round(m.np_w) if m.np_w else None,
                "if": round(m.if_, 2) if m.if_ else None,
                "tss": round(m.tss) if m.tss is not None else None,
                "tss_source": m.tss_source,
                "ef": round(m.ef, 2) if m.ef else None,
                "decoupling_pct": round(m.decoupling_pct, 1)
                if m.decoupling_pct is not None
                else None,
                "hr_lag_s": m.hr_lag_s,
                "classification": cls,
                "classification_zh": CLASS_ZH.get(cls or "", cls),
                "status": m.status,
                "next": m.next_recommendation,
                "climbs": len(m.climbs or []) if isinstance(m.climbs, list) else 0,
                "explanation": m.explanation,
            }
        )
        if out["load"] is None and m.tss is not None:
            out["load"], out["load_source"] = round(m.tss, 1), "cyp"
    return out


def _readiness(session: Session, athlete_id: int, day: dt.date) -> dict[str, Any] | None:
    row = session.get(ReadinessDaily, (athlete_id, day))
    if row is None:
        return None
    comps = (row.inputs or {}).get("components", {}) if isinstance(row.inputs, dict) else {}
    total_w = sum(float(c.get("weight", 0)) for c in comps.values()) or 1.0
    return {
        "date": day,
        "score": row.score_0_100,
        "status": row.status,
        "recommendation": row.recommendation,
        "components": [
            {"name": k, "z": c.get("z"), "share": float(c.get("weight", 0)) / total_w}
            for k, c in sorted(comps.items(), key=lambda kv: -abs(kv[1].get("z") or 0))
        ],
        "missing": (row.inputs or {}).get("missing", []),
        "rule_hits": (row.inputs or {}).get("rule_hits", []),
        "explanation": row.explanation,
    }


def _fitness(session: Session, athlete_id: int, day: dt.date) -> dict[str, Any] | None:
    row = session.get(FitnessDaily, (athlete_id, day))
    if row is None:
        return None
    week_ago = session.get(FitnessDaily, (athlete_id, day - dt.timedelta(days=7)))
    ctl = row.ctl_icu if row.ctl_icu is not None else row.ctl_sim
    ctl_prev = None
    if week_ago is not None:
        ctl_prev = week_ago.ctl_icu if week_ago.ctl_icu is not None else week_ago.ctl_sim
    atl = row.atl_icu if row.atl_icu is not None else row.atl_sim
    tsb = row.tsb_icu if row.tsb_icu is not None else row.tsb_sim
    return {
        "date": day,
        "ctl": ctl,
        "atl": atl,
        "tsb": tsb,
        "source": "icu" if row.ctl_icu is not None else "sim",
        "ctl_sim": row.ctl_sim,
        "ramp_rate": (ctl - ctl_prev) if ctl is not None and ctl_prev is not None else None,
        "acwr": row.acwr_7_28,
        "monotony": row.monotony_7,
        "strain": row.strain_7,
        "load": row.load_actual,
    }


def _warnings(fit: dict[str, Any] | None, cfg: AthleteConfig | None) -> list[str]:
    if not fit:
        return []
    out: list[str] = []
    ramp_cap = max((cfg.planner.ramp_cap.values() if cfg else []), default=DEFAULT_RAMP_CAP)
    floor = min((cfg.planner.tsb_floor.values() if cfg else []), default=DEFAULT_TSB_FLOOR)
    if fit.get("acwr") is not None and fit["acwr"] > ACWR_HIGH:
        out.append(
            f"ACWR {fit['acwr']:.2f} > {ACWR_HIGH}：最近一週比過去一個月重很多，受傷風險上升"
        )
    if fit.get("acwr") is not None and fit["acwr"] < ACWR_LOW:
        out.append(f"ACWR {fit['acwr']:.2f} < {ACWR_LOW}：負荷在下降（減量週正常，否則體能會掉）")
    if fit.get("ramp_rate") is not None and fit["ramp_rate"] > ramp_cap:
        out.append(f"CTL 一週升 {fit['ramp_rate']:.1f}，超過上限 {ramp_cap:g}")
    if fit.get("tsb") is not None and fit["tsb"] < floor:
        out.append(f"TSB {fit['tsb']:.1f} 低於下限 {floor:g}")
    if fit.get("monotony") is not None and fit["monotony"] > MONOTONY_HIGH:
        out.append(
            f"單調度 {fit['monotony']:.2f} > {MONOTONY_HIGH}：每天負荷太像，缺少真正的輕鬆日"
        )
    return out


def _plan(session: Session, athlete_id: int, day: dt.date) -> list[dict[str, Any]]:
    ours = session.scalars(
        select(PlannedWorkout)
        .where(PlannedWorkout.athlete_id == athlete_id, PlannedWorkout.date_local == day)
        .order_by(PlannedWorkout.slot)
    ).all()
    out = [
        {
            "source": "cyp",
            "name": w.name,
            "intent": w.intent,
            "target_tss": w.target_tss,
            "duration_min": round((w.target_duration_s or 0) / 60),
            "indoor": w.indoor,
            "status": w.status,
            "explanation": w.explanation,
        }
        for w in ours
        if w.status not in ("cancelled", "superseded")
    ]
    if out:
        return out
    icu = session.scalars(
        select(IcuEvent).where(
            IcuEvent.category == "WORKOUT",
            IcuEvent.start_date_local >= day.isoformat(),
            IcuEvent.start_date_local < (day + dt.timedelta(days=1)).isoformat(),
        )
    ).all()
    return [
        {
            "source": "icu",
            "name": e.name,
            "intent": None,
            "target_tss": e.icu_training_load,
            "duration_min": None,
            "indoor": None,
            "status": "icu",
            "explanation": None,
        }
        for e in icu
    ]


def season_week(cfg: AthleteConfig | None, day: dt.date) -> int | None:
    """1-based season week of ``day`` (``None`` before the season / without config)."""
    if cfg is None or day < cfg.season.start:
        return None
    return (day - cfg.season.start).days // 7 + 1


def _goal(cfg: AthleteConfig | None, day: dt.date, ftp: float | None) -> dict[str, Any] | None:
    if cfg is None:
        return None
    goal = next((g for g in cfg.goals if g.kind == "ftp_target"), None)
    if goal is None:
        return None
    week = season_week(cfg, day)
    nxt = next((c for c in goal.checkpoints if week is None or c.week >= week), None)
    return {
        "name": goal.name,
        "date": goal.date,
        "target_ftp": goal.target.get("ftp"),
        "current_ftp": ftp,
        "gap_w": (goal.target.get("ftp", 0) - ftp) if ftp else None,
        "weeks_left": max((goal.date - day).days // 7, 0),
        "season_week": week,
        "next_checkpoint": {"week": nxt.week, "ftp": nxt.ftp} if nxt else None,
    }


def _trends(reports_dir: Path) -> dict[str, Any] | None:
    return load_latest_report(reports_dir)


def daily_facts(
    session: Session, day: dt.date, *, reports_dir: Path, cfg: AthleteConfig | None
) -> dict[str, Any]:
    """Facts for the daily report of ``day`` (yesterday's training -> today's verdict / plan).

    Raises:
        AnalysisError: no athlete in the store.
    """
    athlete_id = primary_athlete_id(session)
    if athlete_id is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    yesterday = day - dt.timedelta(days=1)
    settings_row = AthleteSettingsRepo(session).effective_on(athlete_id, day)
    ftp = settings_row.ftp if settings_row else None
    fit = _fitness(session, athlete_id, yesterday)
    trends = _trends(reports_dir)
    proposal = (trends or {}).get("ftp_proposal")
    prop_expl = next(
        (e for e in (trends or {}).get("explanations", []) if e.get("key") == "ftp.proposal"),
        None,
    )
    return {
        "kind": "daily",
        "date": day,
        "yesterday": yesterday,
        "season_week": season_week(cfg, day),
        "ftp": ftp,
        "goal": _goal(cfg, day, ftp),
        "activities": [ride_fact(a, m) for a, m in _rides_on(session, yesterday)],
        "readiness": _readiness(session, athlete_id, day),
        "fitness": fit,
        "warnings": _warnings(fit, cfg),
        "plan": _plan(session, athlete_id, day),
        "ftp_proposal": proposal if proposal and proposal.get("proposed_ftp") else None,
        "ftp_proposal_explanation": prop_expl
        if proposal and proposal.get("proposed_ftp")
        else None,
        "trends_as_of": (trends or {}).get("as_of"),
    }


def week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    """Monday..Sunday of ``day``'s ISO week."""
    start = week_start(day)
    return start, start + dt.timedelta(days=6)


def weekly_facts(
    session: Session, week_start: dt.date, *, reports_dir: Path, cfg: AthleteConfig | None
) -> dict[str, Any]:
    """Facts for the weekly review of the ISO week starting ``week_start`` (a Monday).

    Raises:
        AnalysisError: no athlete in the store.
    """
    athlete_id = primary_athlete_id(session)
    if athlete_id is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    start, end = week_bounds(week_start)
    days_out: list[dict[str, Any]] = []
    tiz_rides: list[tid_mod.RideTiz] = []
    hours = load = planned = 0.0
    n_rides = 0
    for i in range(7):
        day = start + dt.timedelta(days=i)
        acts = _rides_on(session, day)
        facts = [ride_fact(a, m) for a, m in acts]
        day_load = sum(f["load"] or 0 for f in facts)
        load += day_load
        hours += sum(f["moving_min"] for f in facts) / 60
        n_rides += sum(1 for f in facts if f["is_ride"])
        plan = _plan(session, athlete_id, day)
        planned += sum(p["target_tss"] or 0 for p in plan)
        for a, m in acts:
            if m is None:
                continue
            picked = tid_mod.tiz_from_metrics(
                m.time_in_zone_power, m.time_in_zone_hr, a.icu_zone_times
            )
            if picked:
                tiz_rides.append(tid_mod.RideTiz(day, picked[0], picked[1]))
        r = _readiness(session, athlete_id, day)
        days_out.append(
            {
                "date": day,
                "activities": facts,
                "load": round(day_load, 1),
                "plan": plan,
                "readiness": {"score": r["score"], "recommendation": r["recommendation"]}
                if r
                else None,
            }
        )
    weeks = tid_mod.weekly_tid(tiz_rides)
    week_tid = weeks[0] if weeks else None
    phase = "base" if season_week(cfg, start) else None
    tid_fact = None
    if week_tid is not None:
        low, mid, high = week_tid.fractions()
        tid_fact = {
            "low": low,
            "mid": mid,
            "high": high,
            "pi": week_tid.polarization_index,
            "model": week_tid.model,
            "target": tid_mod.PHASE_TARGETS.get(phase or ""),
            "explanation": tid_mod.explain_week(week_tid, phase).to_json_dict(),
        }
    fit_start = _fitness(session, athlete_id, start - dt.timedelta(days=1))
    fit_end = _fitness(session, athlete_id, end)
    trends = _trends(reports_dir) or {}
    settings_row = AthleteSettingsRepo(session).effective_on(athlete_id, end)
    ftp = settings_row.ftp if settings_row else None
    expl = {e.get("key"): e for e in trends.get("explanations", [])}
    week_climbs = [
        c
        for c in trends.get("climbs", [])
        if start.isoformat() <= c["latest"]["date"] <= end.isoformat()
    ]
    return {
        "kind": "weekly",
        "week_start": start,
        "week_end": end,
        "iso_week": start.isocalendar(),
        "season_week": season_week(cfg, start),
        "ftp": ftp,
        "goal": _goal(cfg, end, ftp),
        "days": days_out,
        "totals": {
            "hours": round(hours, 1),
            "load": round(load),
            "planned_load": round(planned) if planned else None,
            "n_rides": n_rides,
            "budget_hours": round(cfg.availability.weekly_max_minutes / 60, 1) if cfg else None,
        },
        "tid": tid_fact,
        "fitness_start": fit_start,
        "fitness_end": fit_end,
        "warnings": _warnings(fit_end, cfg),
        "cp_fits": trends.get("cp_fits", {}),
        "ftp_proposal": trends.get("ftp_proposal"),
        "ftp_proposal_explanation": expl.get("ftp.proposal"),
        "durability": [b for b in trends.get("durability_blocks", []) if b.get("median_ratio")],
        "durability_explanation": expl.get("trends.durability"),
        "limiters": [
            {**lim, "explanation": expl.get(f"limiter.{lim['id']}")}
            for lim in trends.get("limiters", [])
        ],
        "planner_bias": trends.get("planner_bias", {}),
        "climbs": week_climbs,
        "trends_as_of": trends.get("as_of"),
    }
