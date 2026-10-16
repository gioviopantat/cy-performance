"""Build a :class:`~cyp.reports.ride_model.RideReport` for one ride (docs/specs/ride-report.md).

Reads persisted results only: the activity and its ``activity_metrics``, the planned workout of
that day, settings / wellness / fitness through :func:`~cyp.dataset.load_dataset`, and the
rides' 1 Hz streams. Longitudinal numbers (FTP status, limiters) come from the same pure function
the trends job runs (:func:`~cyp.analysis.longitudinal.run.compute_trends`), evaluated as of the
ride's day, so the report does not depend on when the job last ran.

This module assembles the report; the sections live in ``ride_intent`` (2), ``ride_goal`` (3)
and ``ride_sections`` (4-7, series). The only verdicts computed are the v1 intent check, the
outdoor context and the goal projection; every threshold is a named constant quoted, with its
value, in the section's explanation. Athlete-facing text comes from
``reports/i18n/ride_report.zh-TW.yaml`` (:func:`~cyp.reports.ride_common.text`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal.run import compute_trends
from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.run import activity_local_date
from cyp.core.errors import NotFoundError
from cyp.core.timeutil import now_utc, parse_iso, to_local
from cyp.dataset import DEFAULT_TZ, load_dataset
from cyp.reports.facts import ride_fact
from cyp.reports.ride_common import METHOD_DOCS, RideInputs, is_indoor, text
from cyp.reports.ride_goal import build_goal
from cyp.reports.ride_intent import build_intent
from cyp.reports.ride_model import (
    Goal,
    Headline,
    Intent,
    MethodUsed,
    RideReport,
    RideSummary,
    Tomorrow,
    Verdict,
)
from cyp.reports.ride_sections import (
    build_climbs,
    build_feel,
    build_limiters,
    build_outdoor,
    build_series,
)
from cyp.settings import AthleteConfig
from cyp.store.models import Activity, ActivityMetrics, Athlete
from cyp.store.streams import StreamStore

__all__ = ["build_ride_report", "text"]


def _start_local(a: Activity) -> dt.datetime:
    """Naive local start: ``start_local`` as recorded, else ``start_utc`` in the ride's tz."""
    if a.start_local:
        return dt.datetime.fromisoformat(a.start_local).replace(tzinfo=None)
    return to_local(parse_iso(a.start_utc), a.tz or DEFAULT_TZ).replace(tzinfo=None)


def _summary(a: Activity, fact: Mapping[str, Any]) -> RideSummary:
    return RideSummary(
        activity_id=a.id,
        name=fact["name"],
        start_local=_start_local(a),
        sport=a.sport_type,
        indoor=is_indoor(a),
        moving_min=fact["moving_min"],
        distance_km=fact["distance_km"],
        elev_gain_m=fact["elev_gain_m"],
        np_w=fact["np_w"],
        if_=fact["if"],
        tss=round(fact["load"]) if fact["load"] is not None else None,
        tss_source=fact["load_source"],
    )


def build_headline(
    intent: Intent | None, goal: Goal | None, tomorrow: Tomorrow | None, fact: Mapping[str, Any]
) -> Headline:
    """Section 1: deterministic sentence from the section verdicts (no LLM).

    The intent clause is the intent section's own headline, so both quote the same numbers.
    """
    parts: list[str] = []
    if intent is None:
        verdict: Verdict = "unplanned"
        cls = fact["classification_zh"] or fact["sport"] or ""
        parts.append(text("headline.unplanned", cls=cls))
    else:
        verdict = intent.verdict
        parts.append(text(f"headline.{verdict}", name=intent.planned_name))
        detail = (intent.explanation or {}).get("headline_zh")
        if detail:
            parts.append(detail + text("headline.end"))
    if goal is not None:
        if goal.projected_date is not None:
            g = text("headline.goal_date", target=goal.target_w, date=goal.projected_date)
            d = goal.projected_shift_days
            if d is not None:
                g += (
                    text("headline.goal_shift_earlier", days=-d)
                    if d < 0
                    else text("headline.goal_shift_later", days=d)
                    if d > 0
                    else text("headline.goal_shift_same")
                )
            parts.append(g + text("headline.end"))
        elif goal.on_track is False:
            parts.append(
                text("headline.goal_after", target=goal.target_w, date=goal.target_date)
                + text("headline.end")
            )
        else:
            parts.append(text("headline.goal_none", target=goal.target_w) + text("headline.end"))
    if tomorrow is not None:
        if tomorrow.after_name is None:
            parts.append(text("headline.tomorrow_rest"))
        else:
            key = "headline.tomorrow_changed" if tomorrow.changed else "headline.tomorrow_same"
            parts.append(text(key, name=tomorrow.after_name))
    return Headline(text_zh="".join(parts), verdict=verdict)


def build_methods(report: RideReport, fact: Mapping[str, Any]) -> list[MethodUsed]:
    """Section 9: one entry per model a number above came from (grade/claim filled later)."""
    ids: list[str] = []
    if fact["np_w"] is not None or fact["tss"] is not None:
        ids.append("coggan_np_if_tss")
    if report.intent is not None:
        ids.append(intent_mod.INTENT_VERSION)
    if report.goal is not None:
        ids += [goal_mod.GOAL_VERSION, "eftp"]
    if report.outdoor is not None:
        ids.append(ctx_mod.CONTEXT_VERSION)
    if report.climbs:
        ids.append("climb_repeats")
    if report.feel.rated:
        ids.append("readiness_v1.1")
    if report.limiters is not None:
        ids.append("limiters")
    if report.tomorrow is not None:
        ids.append("planner")
    return [MethodUsed(model_id=i, doc=METHOD_DOCS[i]) for i in ids]


def build_ride_report(
    session: Session,
    store: StreamStore,
    activity_id: int,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    tomorrow: Tomorrow | None = None,
    rate_url: str | None = None,
    feedback: tuple[float | None, int | None] | None = None,
) -> RideReport:
    """Read persisted results and the ride's streams into a report.

    Computes no new verdicts beyond the v1 intent and outdoor-context rules in the spec.
    ``feedback`` is the web post-ride answer ``(rpe, feel)``; per field it wins over
    intervals.icu's (as in ``services.ride_feedback``).

    Raises:
        NotFoundError: unknown activity.
    """
    a = session.get(Activity, activity_id)
    if a is None:
        raise NotFoundError(f"no activity {activity_id}")
    m = session.get(ActivityMetrics, activity_id)
    fact = ride_fact(a, m)
    day = activity_local_date(a)
    ds = load_dataset(session)
    if ds is not None and cfg is not None:
        ds = ds.with_data_quality(cfg.data_quality.resolved())
    row = next((r for r in ds.activities if r.id == a.id), None) if ds is not None else None
    inp = RideInputs(
        session=session,
        store=store,
        activity=a,
        metrics=m,
        fact=fact,
        day=day,
        cfg=cfg,
        ds=ds,
        row=row,
        trends=compute_trends(ds, day) if ds is not None and row is not None else None,
    )
    intent = build_intent(inp)
    goal = build_goal(inp)
    athlete = session.get(Athlete, a.athlete_id) if a.athlete_id is not None else None
    report = RideReport(
        profile=profile,
        athlete_name=(athlete.name if athlete and athlete.name else profile),
        generated_at=now_utc(),
        ride=_summary(a, fact),
        headline=build_headline(intent, goal, tomorrow, fact),
        intent=intent,
        goal=goal,
        outdoor=build_outdoor(inp, planned_reps=bool(intent and intent.reps_planned)),
        climbs=build_climbs(inp),
        feel=build_feel(inp, feedback, rate_url),
        limiters=build_limiters(inp),
        tomorrow=tomorrow,
        series=build_series(inp.frame),
    )
    report.methods = build_methods(report, fact)
    return report
