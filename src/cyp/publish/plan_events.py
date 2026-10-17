"""A plan run as the calendar events we own (rest days produce no event).

Lives in ``publish`` so the planner never depends on the publishing layer: planning produces a
:class:`~cyp.planning.job.PlanRun`, this module turns it into :class:`EventSpec` payloads
(workout text, explanation footer, suggested climb), the publisher diffs and writes them.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from cyp.core.ids import external_id
from cyp.planning import routes
from cyp.planning.job import PlanRun
from cyp.planning.renderer import render as render_workout
from cyp.planning.templates import ResolvedWorkout, icu_expected_tss
from cyp.publish.events import EventSpec
from cyp.settings import AthleteConfig


def event_specs(
    run: PlanRun,
    *,
    render: Callable[[ResolvedWorkout], str] = render_workout,
    climbs: Sequence[routes.Climb] = (),
) -> list[EventSpec]:
    """Our desired calendar for the horizon (rest days produce no event).

    ``climbs`` (``location.climbs``) adds a ``建議路段`` footer line to long outdoor work.
    """
    out: list[EventSpec] = []
    for d in run.days:
        if d.workout is None:
            continue
        lines: list[str] = []
        if d.explanation is not None:
            lines = [d.explanation.headline_zh] + [r.text_zh for r in d.explanation.because[:2]]
        route = routes.footer_line(climbs, d.workout, d.intent)
        if route:
            lines.append(route)
        footer = "\n\n" + "\n".join(f"# {line}" for line in lines) if lines else ""
        out.append(
            EventSpec(
                external_id=external_id(run.season_key, d.date, 1),
                date=d.date,
                name=d.workout.name_zh,
                description=render(d.workout) + footer,
                indoor=not d.outdoor,
                moving_time_s=d.workout.duration_s,
                target_load=round(d.tss, 1),
                icu_load=round(icu_expected_tss(d.workout), 1),
                tags=[d.intent, d.role],
            )
        )
    return out


def climbs_from_config(cfg: AthleteConfig | None) -> list[routes.Climb]:
    """``location.climbs`` as :class:`cyp.planning.routes.Climb` (empty without config)."""
    if cfg is None:
        return []
    return [
        routes.Climb(c.name_zh, c.minutes_min, c.minutes_max, c.grade_pct, tuple(c.good_for))
        for c in cfg.location.climbs
    ]
