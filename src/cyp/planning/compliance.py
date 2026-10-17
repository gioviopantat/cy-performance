"""Plan vs actual for one planned day (docs/specs/adaptive-planning.md, Phase 0b).

:func:`compliance_record` is the one place that turns "what was planned" and "what happened"
into the ``planned_workouts.compliance`` JSON. The morning run fills it for the last days
(``services/compliance.py``) and ``cyp plan replay`` computes the same record against its own
replayed plans; the weekly progression gate (:mod:`cyp.planning.gate`) reads it.

Fields: ``actual_tss`` (the day's load), ``planned_tss``, ``ratio``, ``reference_tss`` (the
slot's target before any adaptation, ADR-0017), ``intent_verdict`` (from the per-ride intent
check the reports use, ``reports/ride_intent.build_intent``), ``overdone``
(:func:`~cyp.planning.adapt.is_overdone` against max(planned, reference)), ``rpe`` / ``feel``
of the longest rated ride, ``rpe_delta`` (RPE minus the class's table value, before any
personal offset), ``rated_class``, ``skipped`` (a workout was planned and nothing was ridden),
``role`` and ``intent`` of the slot.
"""

from __future__ import annotations

from typing import Any

from cyp.analysis.readiness import expected_rpe_for
from cyp.planning.adapt import is_overdone

COMPLIANCE_VERSION = "compliance_v1"
#: Slots whose quality decides the weekly progression gate (hard sessions, tests, long ride).
KEY_ROLES = frozenset({"hit", "test", "long_ride"})


def compliance_record(
    *,
    role: str,
    intent: str | None,
    planned_tss: float | None,
    reference_tss: float | None,
    actual_tss: float | None,
    rode: bool,
    intent_verdict: str | None,
    rpe: float | None = None,
    feel: int | None = None,
    rated_class: str | None = None,
    rated_minutes: float | None = None,
) -> dict[str, Any]:
    """The compliance JSON of one planned slot (see the module docstring)."""
    planned = planned_tss or 0.0
    ref = max(planned, reference_tss or 0.0)
    actual = actual_tss if rode else None
    delta = (
        round(rpe - expected_rpe_for(rated_class, rated_minutes), 2) if rpe is not None else None
    )
    return {
        "algo_version": COMPLIANCE_VERSION,
        "role": role,
        "intent": intent,
        "planned_tss": round(planned, 1),
        "reference_tss": round(ref, 1),
        "actual_tss": round(actual, 1) if actual is not None else None,
        "ratio": round(actual / planned, 3) if actual is not None and planned > 0 else None,
        "intent_verdict": intent_verdict if rode else None,
        "overdone": is_overdone(actual, ref),
        "rpe": rpe,
        "feel": feel,
        "rpe_delta": delta,
        "rated_class": rated_class if rpe is not None or feel is not None else None,
        "skipped": planned > 0 and not rode,
    }
