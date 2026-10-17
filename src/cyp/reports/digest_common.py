"""Shared pieces of the digest builders: zh-TW text, one period's inputs, methods and grades.

``reports/digest_facts.py`` builds one :class:`DigestInputs` per digest and hands it to the
section builders (``digest_sections``, ``digest_progress``, ``digest_weekly``,
``digest_monthly``). Athlete-facing text comes only from
``reports/i18n/digest_report.zh-TW.yaml`` through :func:`text` (plus the per-ride texts the
reused ride builders produce); numbers are formatted into it here, never by the renderer
(docs/07).

Longitudinal numbers come from :func:`~cyp.analysis.longitudinal.run.compute_trends` evaluated
as of the day they describe (cached per day), so a digest does not depend on when the trends job
last ran.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from cyp.analysis import readiness as readiness_mod
from cyp.analysis.longitudinal import durability as dur_mod
from cyp.analysis.longitudinal.run import TrendsResult, compute_trends
from cyp.analysis.ride.frames import RideFrame
from cyp.core import evidence
from cyp.core.errors import NotFoundError
from cyp.dataset import ActivityRow, Dataset
from cyp.planning.season import SEASON_VERSION, SeasonSkeleton, SeasonWeek
from cyp.reports.facts import ride_fact
from cyp.reports.ride_common import METHOD_CLAIMS, METHOD_DOCS, RideInputs
from cyp.reports.ride_model import MethodUsed
from cyp.settings import AthleteConfig
from cyp.store.models import Activity, ActivityMetrics
from cyp.store.streams import StreamStore

TEXT_PATH = Path(__file__).resolve().parent / "i18n" / "digest_report.zh-TW.yaml"

#: Digest-only model id -> (doc, evidence claim or ``None`` when not graded); the per-ride
#: models reuse ``ride_common.METHOD_DOCS`` / ``METHOD_CLAIMS``.
DIGEST_METHODS: dict[str, tuple[str, str | None]] = {
    "banister_pmc": ("docs/glossary/banister_pmc.md", "banister_pmc.load_history"),
    "tid_3zone": ("docs/glossary/time_in_zone_tid.md", "time_in_zone_tid.phase_targets"),
    readiness_mod.ALGO_VERSION: ("docs/glossary/readiness_v1.md", "readiness_v1.weights"),
    dur_mod.DURABILITY_VERSION: (
        "docs/glossary/decoupling_hr_lag.md",
        "decoupling_hr_lag.durability_kj_buckets",
    ),
    "cp_2p": ("docs/glossary/cp_wprime.md", "cp_wprime.cp_boundary"),
    "power_curve": ("docs/04-analysis-engine.md", None),
    SEASON_VERSION: ("docs/glossary/periodization_3_1.md", "periodization_3_1.ratio_3_1"),
    "research_review": ("docs/specs/research-review.md", None),
    "gate_v1": ("docs/adr/0018-progression-gate.md", "planning.progression_gate"),
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


def join(parts: Iterable[str]) -> str:
    """Non-empty clauses joined with the zh-TW clause separator."""
    return text("common.sep").join(p for p in parts if p)


def list_join(parts: Iterable[str]) -> str:
    """Items joined with the zh-TW list separator."""
    return text("common.list_sep").join(parts)


def days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    """Every day from ``start`` to ``end`` inclusive."""
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def method(model_id: str) -> MethodUsed:
    """One section-9/11 entry: the model, its doc and the evidence grade of its claim."""
    if model_id in DIGEST_METHODS:
        doc, claim_id = DIGEST_METHODS[model_id]
    else:
        doc, claim_id = METHOD_DOCS[model_id], METHOD_CLAIMS[model_id]
    if claim_id is None:
        return MethodUsed(model_id=model_id, doc=doc)
    claim = evidence.get(claim_id)
    return MethodUsed(
        model_id=model_id,
        claim_id=claim_id,
        grade=claim.grade,
        citation=claim.citations[0] if claim.citations else None,
        doc=doc,
    )


@dataclass
class DigestInputs:
    """Everything the section builders read for one digest (built once by ``digest_facts``)."""

    session: Session
    store: StreamStore
    cfg: AthleteConfig | None
    ds: Dataset
    profile: str
    athlete_name: str
    #: The profile's reports directory (``facts.weekly_facts`` reads the trends JSON there).
    reports_dir: Path
    #: The athlete's local date when the digest is built; days from here on are not judged.
    today: dt.date
    skeleton: SeasonSkeleton | None
    #: Rides the athlete tagged casual (``services.ride_feedback``): not graded.
    casual: frozenset[int] = frozenset()
    _trends: dict[dt.date, TrendsResult | None] = field(default_factory=dict)
    _rides: dict[int, RideInputs] = field(default_factory=dict)
    #: Streams shared by every ride's inputs (climb history and heat baselines overlap).
    _frames: dict[int, RideFrame | None] = field(default_factory=dict)

    def week_of(self, day: dt.date) -> SeasonWeek | None:
        """The season week containing ``day`` (``None`` outside the season / without config)."""
        return self.skeleton.week_of(day) if self.skeleton is not None else None

    def phase_of(self, day: dt.date) -> str | None:
        """Season phase of ``day``'s week (``base`` / ``build`` / ``threshold`` / ``test``)."""
        w = self.week_of(day)
        return w.phase if w is not None else None

    def trends_at(self, day: dt.date) -> TrendsResult | None:
        """Every longitudinal model as of ``day`` (cached; ``None`` before the first ride)."""
        if day not in self._trends:
            has_rides = any(True for _ in self.ds.rides(None, day))
            self._trends[day] = (
                compute_trends(self.ds, day, phase=self.phase_of(day)) if has_rides else None
            )
        return self._trends[day]

    def rides(self, start: dt.date, end: dt.date) -> list[ActivityRow]:
        """Every ride (analysed or not) with ``start <= date <= end``, oldest first."""
        return [a for a in self.ds.activities if a.is_ride and start <= a.date <= end]

    def ride(self, row: ActivityRow) -> RideInputs:
        """The per-ride report inputs of one ride (cached), for the reused ride builders."""
        if row.id not in self._rides:
            a = self.session.get(Activity, row.id)
            if a is None:
                raise NotFoundError(f"no activity {row.id}")
            m = self.session.get(ActivityMetrics, row.id)
            self._rides[row.id] = RideInputs(
                session=self.session,
                store=self.store,
                activity=a,
                metrics=m,
                fact=ride_fact(a, m),
                day=row.date,
                cfg=self.cfg,
                ds=self.ds,
                row=row,
                trends=None,
                _frames=self._frames,
                casual=row.id in self.casual,
            )
        return self._rides[row.id]
