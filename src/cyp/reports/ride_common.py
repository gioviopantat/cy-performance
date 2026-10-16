"""Shared pieces of the per-ride report builder: zh-TW text, the ride's inputs, method docs.

``reports/ride_facts.py`` builds one :class:`RideInputs` per report and hands it to the section
builders (``ride_intent``, ``ride_goal``, ``ride_sections``). Athlete-facing text comes only from
``reports/i18n/ride_report.zh-TW.yaml`` through :func:`text`; numbers are formatted into it here,
never by the renderer (docs/07).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from sqlalchemy.orm import Session

from cyp.analysis.longitudinal import goal as goal_mod
from cyp.analysis.longitudinal.run import TrendsResult
from cyp.analysis.ride import context as ctx_mod
from cyp.analysis.ride import intent as intent_mod
from cyp.analysis.ride.frames import FloatArray, RideFrame, load_frame
from cyp.core.errors import AnalysisError, NotFoundError
from cyp.dataset import ActivityRow, Dataset
from cyp.settings import AthleteConfig
from cyp.store.models import Activity, ActivityMetrics
from cyp.store.streams import StreamStore

TEXT_PATH = Path(__file__).resolve().parent / "i18n" / "ride_report.zh-TW.yaml"

#: Model id -> glossary / doc path (section 9).
METHOD_DOCS: dict[str, str] = {
    "coggan_np_if_tss": "docs/glossary/coggan_np_if_tss.md",
    intent_mod.INTENT_VERSION: "docs/specs/ride-report.md",
    goal_mod.GOAL_VERSION: "docs/glossary/ftp_target_season.md",
    "eftp": "docs/glossary/eftp.md",
    ctx_mod.CONTEXT_VERSION: "docs/specs/ride-report.md",
    "climb_repeats": "docs/04-analysis-engine.md",
    "readiness_v1.1": "docs/glossary/readiness_v1.md",
    "limiters": "docs/04-analysis-engine.md",
    "planner": "docs/glossary/workout_library.md",
}


@cache
def texts() -> dict[str, Any]:
    """The whole zh-TW text file (cached)."""
    data: dict[str, Any] = yaml.safe_load(TEXT_PATH.read_text(encoding="utf-8"))
    return data


def text(key: str, **values: Any) -> str:
    """zh-TW template ``section.name`` formatted with ``values``."""
    section, name = key.split(".", 1)
    template: str = texts()[section][name]
    return template.format(**values)


def join(parts: list[str]) -> str:
    """Clauses joined with the zh-TW clause separator."""
    return text("common.sep").join(parts)


def is_indoor(a: Activity) -> bool:
    """Trainer or virtual ride."""
    return bool(a.trainer) or a.sport_type == "VirtualRide"


@dataclass
class RideInputs:
    """Everything the section builders read for one ride (built once by ``ride_facts``)."""

    session: Session
    store: StreamStore
    activity: Activity
    metrics: ActivityMetrics | None
    fact: dict[str, Any]
    day: dt.date
    cfg: AthleteConfig | None
    ds: Dataset | None
    row: ActivityRow | None
    trends: TrendsResult | None
    _frames: dict[int, RideFrame | None] = field(default_factory=dict)

    @property
    def indoor(self) -> bool:
        """Trainer or virtual ride."""
        return is_indoor(self.activity)

    @property
    def ftp(self) -> float | None:
        """FTP effective on the ride's day."""
        return self.ds.ftp_on(self.day) if self.ds is not None else None

    @property
    def frame(self) -> RideFrame | None:
        """This ride's 1 Hz streams."""
        return self.frame_of(self.activity.id)

    def frame_of(self, activity_id: int) -> RideFrame | None:
        """Streams of any ride (cached; ``None`` when missing or malformed)."""
        if activity_id not in self._frames:
            a = self.session.get(Activity, activity_id)
            trainer = bool(a.trainer) if a is not None else False
            try:
                self._frames[activity_id] = load_frame(self.store, activity_id, trainer=trainer)
            except (NotFoundError, AnalysisError):
                self._frames[activity_id] = None
        return self._frames[activity_id]

    def power_of(self, r: ActivityRow) -> FloatArray | None:
        """Measured power of a ride we may judge with (``None`` without a trusted meter)."""
        if not r.power_ok:
            return None
        f = self.frame_of(r.id)
        if f is None or f.watts is None or not np.isfinite(f.watts).any():
            return None
        return f.watts

    @property
    def power(self) -> FloatArray | None:
        """This ride's trusted power (``None`` without a meter or when it is unreliable)."""
        f = self.frame
        if f is None or f.watts is None or not np.isfinite(f.watts).any():
            return None
        if self.row is not None and self.row.power_unreliable is not None:
            return None
        return f.watts
