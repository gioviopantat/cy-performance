"""Strava <-> intervals.icu activity matcher (docs/02 §3.6, ADR-0003 §4).

Two sources can describe the same ride. The matcher collapses such pairs onto the intervals.icu
row (icu is primary) and labels every row with how its ids were joined:

``strava_id``      icu ``Activity.strava_id`` (or, for Strava-origin icu stubs, the icu id itself,
                   which *is* the Strava id) equals a Strava row's id.
``time_window``    no id in common; ``start_utc`` within ±120 s and ``moving_s`` **or**
                   ``elapsed_s`` within ±2 % (icu and Strava compute moving time differently
                   on the same FIT file; elapsed time is identical).
``single_source``  only one source knows the activity.

Merging copies Strava-only data (raw payload, summary columns icu lacks, segment efforts,
zones, the Parquet pointer when icu has no streams) onto the icu row, re-points children, and
deletes the duplicate. Streams from two sources are never mixed: when icu already has streams
the Strava copy is removed (ADR-0003 §2).

:meth:`Matcher.rematch_all` is idempotent and migration-free: it can be run on an existing
database at any time to merge duplicates created by sync ordering and to repair labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.timeutil import parse_iso
from cyp.logging import get_logger
from cyp.store.models import (
    Activity,
    ActivityInterval,
    ActivityMetrics,
    ActivityZones,
    PlannedWorkout,
    SegmentEffort,
    StreamFile,
)
from cyp.store.runs import RunContext
from cyp.store.streams import StreamStore

log = get_logger(__name__)

JOB = "match"
START_TOLERANCE_S = 120
DURATION_TOLERANCE = 0.02

#: Summary columns a Strava row may contribute. For a normal icu row only ``None`` values are
#: filled; for an icu *stub* (Strava-origin activity hidden by the API) Strava wins outright.
STRAVA_SUMMARY_COLUMNS: tuple[str, ...] = (
    "sport_type",
    "is_ride",
    "name",
    "description",
    "start_utc",
    "start_local",
    "tz",
    "moving_s",
    "elapsed_s",
    "distance_m",
    "elev_gain_m",
    "trainer",
    "commute",
    "race",
    "manual",
    "has_power",
    "has_hr",
    "has_cadence",
    "device_name",
    "gear_id",
    "gear_name",
    "avg_w",
    "np_w",
    "max_w",
    "kj",
    "avg_hr",
    "max_hr",
    "avg_cad",
)
_STRAVA_STAGE_FLAGS = ("detail", "efforts", "zones", "strava_detail")
#: Negative-cache markers (``"<iso ts> <status>"``) carried verbatim onto the primary.
_STRAVA_FAILED_FLAGS = ("strava_detail_failed", "strava_streams_failed")


@dataclass
class MatchReport:
    """Counters of one :meth:`Matcher.rematch_all` (mirrored into ``job_runs.counts``)."""

    merged_by_strava_id: int = 0
    merged_by_time_window: int = 0
    relabelled: int = 0
    streams_moved: int = 0
    streams_dropped: int = 0
    rows_by_method: dict[str, int] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """Flat integer counters."""
        out = {
            "merged_by_strava_id": self.merged_by_strava_id,
            "merged_by_time_window": self.merged_by_time_window,
            "relabelled": self.relabelled,
            "streams_moved": self.streams_moved,
            "streams_dropped": self.streams_dropped,
        }
        out.update({f"rows_{k}": v for k, v in sorted(self.rows_by_method.items())})
        return out


def is_icu_stub(row: Activity) -> bool:
    """True for icu rows whose payload is the Strava-origin stub (no ``type``, no metrics)."""
    raw = row.raw_intervals_json
    if not isinstance(raw, dict) or row.intervals_id is None:
        return False
    return raw.get("type") is None and str(raw.get("source") or "").upper() == "STRAVA"


def effective_strava_id(row: Activity) -> int | None:
    """``strava_id`` or, for a Strava-origin icu stub, the numeric icu id (same number)."""
    if row.strava_id is not None:
        return row.strava_id
    if row.intervals_id and row.intervals_id.isdigit() and is_icu_stub(row):
        return int(row.intervals_id)
    return None


def within_time_window(
    a: Activity,
    b: Activity,
    *,
    start_tolerance_s: int = START_TOLERANCE_S,
    duration_tolerance: float = DURATION_TOLERANCE,
) -> bool:
    """Rule 2 of docs/02 §3.6: start ±120 s and moving **or** elapsed duration within ±2 %."""
    if not a.start_utc or not b.start_utc:
        return False
    delta = abs((parse_iso(a.start_utc) - parse_iso(b.start_utc)).total_seconds())
    if delta > start_tolerance_s:
        return False
    for col in ("moving_s", "elapsed_s"):
        x, y = getattr(a, col), getattr(b, col)
        if x and y and abs(x - y) <= duration_tolerance * max(x, y):
            return True
    return False


class Matcher:
    """Merge duplicates and label ``match_method`` (see module docstring)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        stream_store: StreamStore,
        *,
        start_tolerance_s: int = START_TOLERANCE_S,
        duration_tolerance: float = DURATION_TOLERANCE,
    ) -> None:
        self.factory = factory
        self.streams = stream_store
        self.start_tolerance_s = start_tolerance_s
        self.duration_tolerance = duration_tolerance

    # ------------------------------------------------------------------ public API

    def rematch_all(self, ctx: RunContext | None = None) -> MatchReport:
        """Run both rules over the whole table, then relabel. Safe to repeat."""
        report = MatchReport()
        with self.factory() as s:
            self._merge_by_strava_id(s, report)
            s.commit()
            self._merge_by_time_window(s, report)
            s.commit()
            self._relabel(s, report)
            s.commit()
            report.rows_by_method = self.rows_by_method(s)
        if ctx is not None:
            for key, value in report.counts().items():
                ctx.incr(key, value)
        log.info("matcher.done", **report.counts())
        return report

    @staticmethod
    def rows_by_method(session: Session) -> dict[str, int]:
        """``match_method -> row count``."""
        out: dict[str, int] = {}
        for method in session.scalars(select(Activity.match_method)):
            out[method or "single_source"] = out.get(method or "single_source", 0) + 1
        return out

    # ------------------------------------------------------------------ rule 1: strava_id

    def _merge_by_strava_id(self, s: Session, report: MatchReport) -> None:
        icu_rows = list(s.scalars(select(Activity).where(Activity.intervals_id.is_not(None))))
        for icu in icu_rows:
            sid = effective_strava_id(icu)
            if sid is None:
                continue
            dup = s.scalars(
                select(Activity).where(
                    Activity.strava_id == sid,
                    Activity.id != icu.id,
                    Activity.intervals_id.is_(None),
                )
            ).first()
            if dup is None:
                if icu.strava_id is None:
                    icu.strava_id = sid  # stub whose Strava twin is not synced (yet)
                    icu.match_method = "strava_id"
                    report.relabelled += 1
                continue
            self._merge(s, primary=icu, dup=dup, method="strava_id", report=report)
            report.merged_by_strava_id += 1

    # ------------------------------------------------------------------ rule 2: time window

    def _merge_by_time_window(self, s: Session, report: MatchReport) -> None:
        icu_only = list(
            s.scalars(
                select(Activity)
                .where(Activity.intervals_id.is_not(None), Activity.strava_id.is_(None))
                .order_by(Activity.start_utc)
            )
        )
        strava_only = list(
            s.scalars(
                select(Activity)
                .where(Activity.strava_id.is_not(None), Activity.intervals_id.is_(None))
                .order_by(Activity.start_utc)
            )
        )
        if not icu_only or not strava_only:
            return
        candidates: list[tuple[float, Activity, Activity]] = []
        for icu in icu_only:
            for strava in strava_only:
                if within_time_window(
                    icu,
                    strava,
                    start_tolerance_s=self.start_tolerance_s,
                    duration_tolerance=self.duration_tolerance,
                ):
                    delta = abs(
                        (parse_iso(icu.start_utc) - parse_iso(strava.start_utc)).total_seconds()
                    )
                    candidates.append((delta, icu, strava))
        # Closest start first; every row is used at most once.
        used: set[int] = set()
        for _delta, icu, strava in sorted(candidates, key=lambda c: (c[0], c[1].id, c[2].id)):
            if icu.id in used or strava.id in used:
                continue
            used.update((icu.id, strava.id))
            self._merge(s, primary=icu, dup=strava, method="time_window", report=report)
            report.merged_by_time_window += 1

    # ------------------------------------------------------------------ relabel

    def _relabel(self, s: Session, report: MatchReport) -> None:
        for row in s.scalars(select(Activity)):
            both = row.strava_id is not None and row.intervals_id is not None
            if both:
                wanted = row.match_method if row.match_method == "time_window" else "strava_id"
            else:
                wanted = "single_source"
            if row.match_method != wanted:
                row.match_method = wanted
                report.relabelled += 1

    # ------------------------------------------------------------------ merge

    def _merge(
        self, s: Session, *, primary: Activity, dup: Activity, method: str, report: MatchReport
    ) -> None:
        log.info(
            "matcher.merge",
            method=method,
            primary_id=primary.id,
            dup_id=dup.id,
            strava_id=dup.strava_id,
            intervals_id=primary.intervals_id,
        )
        strava_id = dup.strava_id
        dup.strava_id = None  # unique constraint: release before the primary claims it
        s.flush()
        primary.strava_id = strava_id
        primary.match_method = method

        stub = is_icu_stub(primary)
        for col in STRAVA_SUMMARY_COLUMNS:
            value = getattr(dup, col)
            if value is None:
                continue
            if stub or getattr(primary, col) is None:
                setattr(primary, col, value)
        if dup.raw_strava_json is not None:
            primary.raw_strava_json = dup.raw_strava_json
        if primary.athlete_id is None:
            primary.athlete_id = dup.athlete_id

        flags = dict(primary.stage_flags or {})
        dup_flags = dup.stage_flags or {}
        for name in _STRAVA_STAGE_FLAGS:
            if dup_flags.get(name):
                flags[name] = True
        for name in _STRAVA_FAILED_FLAGS:
            if dup_flags.get(name) and name not in flags:
                flags[name] = dup_flags[name]
        # Strava detail (segment efforts) is still owed if the duplicate never fetched it.
        primary.pending_detail = bool(dup.pending_detail)

        self._move_children(s, primary, dup)
        moved = self._move_streams(s, primary, dup, report)
        if moved:
            flags["streams"] = True
            flags.pop("streams_skipped", None)
            primary.pending_streams = False
        primary.stage_flags = flags
        primary.pending_analysis = True
        s.delete(dup)
        s.flush()

    def _move_children(self, s: Session, primary: Activity, dup: Activity) -> None:
        for effort in s.scalars(select(SegmentEffort).where(SegmentEffort.activity_id == dup.id)):
            effort.activity_id = primary.id
        have_zone_types = set(
            s.scalars(
                select(ActivityZones.zone_type).where(ActivityZones.activity_id == primary.id)
            )
        )
        for zone in s.scalars(select(ActivityZones).where(ActivityZones.activity_id == dup.id)):
            if zone.zone_type in have_zone_types:
                s.delete(zone)
            else:
                zone.activity_id = primary.id
        have_interval_sources = set(
            s.scalars(
                select(ActivityInterval.source).where(ActivityInterval.activity_id == primary.id)
            )
        )
        for iv in s.scalars(select(ActivityInterval).where(ActivityInterval.activity_id == dup.id)):
            if iv.source in have_interval_sources:
                s.delete(iv)
            else:
                iv.activity_id = primary.id
        metrics = s.get(ActivityMetrics, dup.id)
        if metrics is not None:
            s.delete(metrics)  # recomputed for the primary (pending_analysis)
        for pw in s.scalars(
            select(PlannedWorkout).where(PlannedWorkout.executed_activity_id == dup.id)
        ):
            pw.executed_activity_id = primary.id
        s.flush()

    def _move_streams(
        self, s: Session, primary: Activity, dup: Activity, report: MatchReport
    ) -> bool:
        """Re-point the duplicate's Parquet to the primary when it has none; else drop it."""
        dup_file = s.get(StreamFile, dup.id)
        primary_has = s.get(StreamFile, primary.id) is not None or self.streams.exists(primary.id)
        if dup_file is None:
            return False
        if primary_has:
            s.delete(dup_file)
            s.flush()
            self.streams.delete(dup.id)
            _unlink(dup_file.raw_json_path)
            report.streams_dropped += 1
            return False
        src = Path(dup_file.path)
        target = self.streams.path_for(primary.id)
        if src.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            src.replace(target)
        raw_target: str | None = None
        if dup_file.raw_json_path:
            raw_src = Path(dup_file.raw_json_path)
            raw_dst = raw_src.with_name(raw_src.name.replace(str(dup.id), str(primary.id), 1))
            if raw_src.is_file():
                raw_src.replace(raw_dst)
            raw_target = str(raw_dst)
        values: dict[str, Any] = {
            "source": dup_file.source,
            "path": str(target),
            "columns": dup_file.columns,
            "hz": dup_file.hz,
            "original_size": dup_file.original_size,
            "resolution": dup_file.resolution,
            "n_samples": dup_file.n_samples,
            "raw_json_path": raw_target,
        }
        s.delete(dup_file)
        s.flush()
        s.add(StreamFile(activity_id=primary.id, **values))
        s.flush()
        report.streams_moved += 1
        return True


def _unlink(path: str | None) -> None:
    if path:
        p = Path(path)
        if p.is_file():
            p.unlink()
