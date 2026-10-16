"""Build a :class:`~cyp.reports.ride_model.RideReport` for one ride (docs/specs/ride-report.md).

Stub: the signature is the contract between the watcher, the builder and the PDF renderer.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from cyp.reports.ride_model import RideReport, Tomorrow
from cyp.settings import AthleteConfig
from cyp.store.streams import StreamStore


def build_ride_report(
    session: Session,
    store: StreamStore,
    activity_id: int,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    tomorrow: Tomorrow | None = None,
    rate_url: str | None = None,
) -> RideReport:
    """Read persisted results and the ride's streams into a report.

    Computes no new verdicts beyond the v1 intent and outdoor-context rules in the spec.
    """
    raise NotImplementedError
