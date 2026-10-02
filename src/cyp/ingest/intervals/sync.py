"""intervals.icu sync job (docs/01 §5.2, docs/02 §3, ADR-0003).

Stages (each wrapped in :func:`cyp.jobs.runs.job_run`, each independently runnable):

``athlete``       ``GET /athlete`` + ``/sport-settings`` -> ``athletes`` +
                  ``athlete_settings_history`` (new row only when values change)
``activities``    incremental by ``sync_cursors['intervals/activities_newest']`` with a 3-day
                  overlap (activities get edited); streams -> Parquet, intervals -> table
``wellness``      ``wellness_daily`` + ``fitness_daily.*_icu`` mirror
``power_curves``  42d / 90d / season snapshots (+ ``mmp-model`` fit)
``events``        ``icu_events`` raw calendar mirror + fitness-model events

:meth:`IntervalsSyncer.backfill` pages the activity and wellness windows month by month and
persists the cursor after every page, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.activity import RIDE_SPORT_TYPES
from cyp.core.errors import IngestError, SchemaError
from cyp.core.timeutil import DEFAULT_TZ, tz
from cyp.ingest.intervals import mapping
from cyp.ingest.intervals.client import POWER_CURVE_WINDOWS, IntervalsClient
from cyp.ingest.intervals.streams import icu_streams_to_parquet_frame
from cyp.jobs.runs import RunContext, job_run
from cyp.logging import get_logger
from cyp.store.models import Activity, StreamFile
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.athlete_settings import AthleteSettingsRepo
from cyp.store.repo.events import IcuEventRepo
from cyp.store.repo.intervals import ActivityIntervalRepo
from cyp.store.repo.power_curves import PowerCurveRepo
from cyp.store.repo.sync_cursors import SyncCursorRepo
from cyp.store.repo.wellness import WellnessRepo
from cyp.store.streams import StreamStore

log = get_logger(__name__)

SOURCE = "intervals"
CURSOR_ACTIVITIES = "activities_newest"
CURSOR_WELLNESS = "wellness_newest"
CURSOR_ATHLETE_ID = "athlete_id"
CURSOR_BACKFILL_PROGRESS = "backfill_progress"  # "<oldest_wanted>|<last page start>"

ACTIVITY_OVERLAP_DAYS = 3
WELLNESS_OVERLAP_DAYS = 7
EVENTS_PAST_DAYS = 14
EVENTS_FUTURE_DAYS = 365
DEFAULT_INITIAL_DAYS = 90
BACKFILL_PAGE_DAYS = 31

JOB_PREFIX = "sync:icu:"
STAGES: tuple[str, ...] = ("athlete", "activities", "wellness", "power_curves", "events")

JsonDict = dict[str, Any]


@dataclass
class SyncOptions:
    """Knobs for one sync run."""

    fetch_streams: bool = True
    fetch_intervals: bool = True
    initial_days: int = DEFAULT_INITIAL_DAYS
    activity_overlap_days: int = ACTIVITY_OVERLAP_DAYS
    wellness_overlap_days: int = WELLNESS_OVERLAP_DAYS
    events_past_days: int = EVENTS_PAST_DAYS
    events_future_days: int = EVENTS_FUTURE_DAYS
    power_curve_windows: dict[str, str] = field(default_factory=lambda: dict(POWER_CURVE_WINDOWS))
    sport: str = "Ride"
    max_gap_s: int = 10


@dataclass
class SyncReport:
    """Counters per stage, as recorded in ``job_runs.counts``."""

    stages: dict[str, dict[str, int]] = field(default_factory=dict)

    def total(self, key: str) -> int:
        """Sum of ``key`` across stages."""
        return sum(c.get(key, 0) for c in self.stages.values())


class IntervalsSyncer:
    """Pull intervals.icu data into the store. One instance per run; not thread-safe."""

    def __init__(
        self,
        client: IntervalsClient,
        factory: sessionmaker[Session],
        stream_store: StreamStore,
        *,
        options: SyncOptions | None = None,
        timezone: str = DEFAULT_TZ,
        clock: Callable[[], dt.datetime] | None = None,
        log_path: str | None = None,
    ) -> None:
        self.client = client
        self.factory = factory
        self.streams = stream_store
        self.options = options or SyncOptions()
        self.timezone = timezone
        self._clock = clock or (lambda: dt.datetime.now(tz(timezone)))
        self.log_path = log_path
        self._athlete_pk: int | None = None

    # ------------------------------------------------------------------ public API

    def now_local(self) -> dt.datetime:
        """Current athlete-local time (injectable for tests)."""
        return self._clock()

    def today_local(self) -> dt.date:
        """Today's date in the athlete's zone."""
        return self.now_local().date()

    def run(self, stages: tuple[str, ...] = STAGES) -> SyncReport:
        """Incremental sync of the given stages (default: all, in dependency order)."""
        report = SyncReport()
        for stage in stages:
            if stage not in STAGES:
                raise ValueError(f"unknown stage {stage!r}; expected one of {STAGES}")
            with job_run(JOB_PREFIX + stage, self.factory, log_path=self.log_path) as ctx:
                getattr(self, f"sync_{stage}")(ctx)
                ctx.rate_limit_snapshot = self.client.rate_limit.snapshot()
                report.stages[stage] = dict(ctx.counts)
        return report

    def backfill(self, days: int, *, stages: tuple[str, ...] = STAGES) -> SyncReport:
        """Import ``days`` of history, paging activities + wellness by month, cursor per page.

        Resumable: ``sync_cursors['intervals/backfill_progress']`` records
        ``"<oldest_wanted>|<last page start>"``; a rerun asking for the same oldest day continues
        from that page (minus the edit overlap), a different span starts over, and the cursor is
        cleared once the present is reached. ``activities_newest`` / ``wellness_newest`` are
        advanced only by the page that reaches today.
        """
        report = SyncReport()
        today = self.today_local()
        oldest_wanted = today - dt.timedelta(days=days)
        if "athlete" in stages:
            with job_run(JOB_PREFIX + "athlete", self.factory, log_path=self.log_path) as ctx:
                self.sync_athlete(ctx)
                report.stages["athlete"] = dict(ctx.counts)
        if "activities" in stages or "wellness" in stages:
            with job_run("backfill:icu", self.factory, log_path=self.log_path) as ctx:
                for page_start, page_end in self._backfill_pages(oldest_wanted, today):
                    if "activities" in stages:
                        self._sync_activity_window(ctx, page_start, page_end)
                    if "wellness" in stages:
                        self._sync_wellness_window(ctx, page_start, page_end)
                    with self.factory() as s:
                        cursors = SyncCursorRepo(s)
                        if page_end >= today:
                            cursors.set(SOURCE, CURSOR_ACTIVITIES, today.isoformat())
                            cursors.set(SOURCE, CURSOR_WELLNESS, today.isoformat())
                            cursors.delete(SOURCE, CURSOR_BACKFILL_PROGRESS)
                        else:
                            cursors.set(
                                SOURCE,
                                CURSOR_BACKFILL_PROGRESS,
                                f"{oldest_wanted.isoformat()}|{page_start.isoformat()}",
                            )
                        s.commit()
                    ctx.incr("pages")
                ctx.rate_limit_snapshot = self.client.rate_limit.snapshot()
                report.stages["backfill"] = dict(ctx.counts)
        for stage in ("power_curves", "events"):
            if stage in stages:
                with job_run(JOB_PREFIX + stage, self.factory, log_path=self.log_path) as ctx:
                    getattr(self, f"sync_{stage}")(ctx)
                    report.stages[stage] = dict(ctx.counts)
        return report

    # ------------------------------------------------------------------ stage: athlete

    def sync_athlete(self, ctx: RunContext) -> int:
        """Upsert ``athletes`` and append ``athlete_settings_history`` when values changed."""
        athlete_raw = self.client.get_athlete()
        icu_id = self.client.resolve_athlete_id()
        settings = self.client.get_sport_settings()
        sport = mapping.pick_sport_settings(settings, self.options.sport)
        with self.factory() as s:
            repo = AthleteSettingsRepo(s)
            athlete = repo.upsert_athlete(icu_id, mapping.athlete_row(athlete_raw))
            SyncCursorRepo(s).set(SOURCE, CURSOR_ATHLETE_ID, icu_id)
            self._athlete_pk = athlete.id
            ctx.incr("athlete")
            if sport is not None:
                values = mapping.settings_history_values(sport, athlete_raw)
                added = repo.append_if_changed(
                    athlete.id,
                    self.today_local(),
                    values,
                    source="icu_sport_settings",
                    raw_json={"sport_settings": sport, "all_sport_settings": settings},
                )
                if added is not None:
                    ctx.incr("settings_history_added")
                    log.info(
                        "intervals.settings_changed", ftp=values.get("ftp"), eftp=values.get("eftp")
                    )
            s.commit()
            return athlete.id

    def _athlete_id(self) -> int:
        """Internal ``athletes.id`` (runs the athlete stage lazily when needed)."""
        if self._athlete_pk is not None:
            return self._athlete_pk
        with self.factory() as s:
            row = AthleteSettingsRepo(s).get_by_intervals_id(self.client.resolve_athlete_id())
            if row is not None:
                self._athlete_pk = row.id
                return row.id
        ctx = RunContext(run_id=-1, job="sync:icu:athlete(implicit)")
        return self.sync_athlete(ctx)

    # ------------------------------------------------------------------ stage: activities

    def sync_activities(self, ctx: RunContext) -> None:
        """Incremental activity sync from the cursor (minus overlap) to now."""
        today = self.today_local()
        with self.factory() as s:
            cursor = SyncCursorRepo(s).get(SOURCE, CURSOR_ACTIVITIES)
        if cursor:
            oldest = dt.date.fromisoformat(cursor[:10]) - dt.timedelta(
                days=self.options.activity_overlap_days
            )
        else:
            oldest = today - dt.timedelta(days=self.options.initial_days)
        newest = today + dt.timedelta(days=1)  # tomorrow: catch rides logged late in the day
        self._sync_activity_window(ctx, oldest, newest)
        with self.factory() as s:
            SyncCursorRepo(s).set(SOURCE, CURSOR_ACTIVITIES, today.isoformat())
            s.commit()

    def _sync_activity_window(self, ctx: RunContext, oldest: dt.date, newest: dt.date) -> None:
        athlete_pk = self._athlete_id()
        raws = self.client.list_activities(oldest, newest)
        ctx.incr("activities_listed", len(raws))
        for raw in sorted(raws, key=lambda r: str(r.get("start_date_local") or "")):
            if not raw.get("id"):
                ctx.incr("activities_skipped_stub")
                continue
            try:
                self._ingest_activity(ctx, raw, athlete_pk)
            except (IngestError, SchemaError) as exc:
                ctx.incr("activities_failed")
                log.error("intervals.activity_failed", activity_id=raw.get("id"), error=str(exc))

    def _ingest_activity(self, ctx: RunContext, raw: JsonDict, athlete_pk: int) -> None:
        row = mapping.activity_row(raw, athlete_id=athlete_pk, default_tz=self.timezone)
        strava_origin = mapping.is_strava_origin(raw)
        streams_available = mapping.has_streams(raw) and not strava_origin
        is_ride = row["sport_type"] in RIDE_SPORT_TYPES
        want_streams = self.options.fetch_streams and is_ride and streams_available
        with self.factory() as s:
            repo = ActivityRepo(s)
            existing = repo.get_by_intervals_id(row["intervals_id"])
            if existing is None and row.get("strava_id") is not None:
                # Strava synced this ride first: attach the icu id to that row (docs/02 §3.6).
                existing = repo.get_by_strava_id(int(row["strava_id"]))
            is_new = existing is None
            joined = existing is not None and existing.intervals_id is None
            changed = existing is None or existing.raw_intervals_json != raw
            # A Strava-origin stub must not clobber what the Strava sync already stored.
            twin_filled = existing is not None and existing.raw_strava_json is not None
            reduced = mapping.is_stub(raw) and twin_filled
            if reduced:
                row = {k: v for k, v in row.items() if k in mapping.STUB_ROW_KEYS}
                row["match_method"] = "strava_id"
            if existing is not None and not changed:
                ctx.incr("activities_unchanged")
                if not (want_streams and existing.pending_streams):
                    return
                # Unchanged, but a previous run never got its streams: retry below.
                activity = existing
                activity_pk = existing.id
                s.commit()
            else:
                activity = repo.upsert(row)
                activity_pk = activity.id
                if joined:
                    ctx.incr("activities_joined_strava")
                if not reduced:
                    # The list payload *is* the full detail object for icu.
                    repo.mark_done(activity, "detail")
                activity.pending_analysis = True
                if (not streams_available or not is_ride) and not reduced:
                    # Nothing to fetch, ever: close the queue and record why.
                    activity.pending_streams = False
                    flags = dict(activity.stage_flags or {})
                    flags["streams"] = False
                    flags["streams_skipped"] = "strava_origin" if strava_origin else "no_streams"
                    activity.stage_flags = flags
                    ctx.incr("streams_skipped_strava" if strava_origin else "streams_skipped_none")
                s.commit()
                ctx.incr("activities_new" if is_new else "activities_updated")

        if want_streams and self._needs_streams(activity_pk):
            self._fetch_streams(ctx, activity_pk, row["intervals_id"])
        if changed and self.options.fetch_intervals and not strava_origin and is_ride:
            self._fetch_intervals(ctx, activity_pk, row["intervals_id"])

    def refetch_streams(self, activity_ids: Iterable[int] | None = None) -> dict[str, int]:
        """Re-download and rewrite the Parquet streams of already-ingested icu rides.

        Maintenance path for when the stream mapping changes. ``activity_ids`` are internal
        ``activities.id`` values; ``None`` means every activity whose ``stream_files.source`` is
        ``"intervals"``. Activities without an ``intervals_id`` are skipped. Each rewrite marks the
        activity ``pending_analysis`` so the next analyze run recomputes from the new file.
        Returns the run counters (``streams_written``, ``refetch_failed``, ...).
        """
        with self.factory() as s:
            stmt = select(Activity.id, Activity.intervals_id)
            if activity_ids is None:
                stmt = stmt.join(StreamFile, StreamFile.activity_id == Activity.id).where(
                    StreamFile.source == SOURCE
                )
            else:
                stmt = stmt.where(Activity.id.in_(list(activity_ids)))
            targets = [(int(pk), icu_id) for pk, icu_id in s.execute(stmt.order_by(Activity.id))]
        with job_run(JOB_PREFIX + "refetch_streams", self.factory, log_path=self.log_path) as ctx:
            for activity_pk, icu_id in targets:
                if not icu_id:
                    ctx.incr("refetch_skipped_no_icu_id")
                    continue
                try:
                    self._fetch_streams(ctx, activity_pk, str(icu_id))
                except (IngestError, SchemaError) as exc:
                    ctx.incr("refetch_failed")
                    log.error("intervals.refetch_failed", activity_id=activity_pk, error=str(exc))
            ctx.rate_limit_snapshot = self.client.rate_limit.snapshot()
            return dict(ctx.counts)

    def _needs_streams(self, activity_pk: int) -> bool:
        with self.factory() as s:
            act = ActivityRepo(s).get(activity_pk)
            return bool(act is not None and act.pending_streams) or not self.streams.exists(
                activity_pk
            )

    def _fetch_streams(self, ctx: RunContext, activity_pk: int, icu_id: str) -> None:
        payload = self.client.get_activity_streams(icu_id)
        if not payload:
            ctx.incr("streams_empty")
            with self.factory() as s:
                repo = ActivityRepo(s)
                act = repo.get(activity_pk)
                if act is not None:
                    act.pending_streams = False
                    flags = dict(act.stage_flags or {})
                    flags["streams"] = False
                    flags["streams_skipped"] = "empty_payload"
                    act.stage_flags = flags
                s.commit()
            return
        frame, info = icu_streams_to_parquet_frame(payload, max_gap_s=self.options.max_gap_s)
        path = self.streams.write(activity_pk, frame)
        raw_path: str | None = None
        if info.resampled:
            raw_dir = self.streams.root / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_file = raw_dir / f"{activity_pk}.intervals.json"
            raw_file.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            raw_path = str(raw_file)
        with self.factory() as s:
            repo = ActivityRepo(s)
            repo.upsert_stream_file(
                activity_pk,
                {
                    "source": "intervals",
                    "path": str(path),
                    "columns": frame.columns,
                    "hz": 1.0,
                    "original_size": info.n_original,
                    "resolution": info.resolution_label,
                    "n_samples": info.n_samples,
                    "raw_json_path": raw_path,
                },
            )
            act = repo.get(activity_pk)
            if act is not None:
                repo.mark_done(act, "streams")
                act.pending_analysis = True
            s.commit()
        ctx.incr("streams_written")
        ctx.incr("stream_samples", info.n_samples)

    def _fetch_intervals(self, ctx: RunContext, activity_pk: int, icu_id: str) -> None:
        payload = self.client.get_activity_intervals(icu_id)
        rows = mapping.interval_rows(payload)
        with self.factory() as s:
            ActivityIntervalRepo(s).replace(activity_pk, "icu", rows)
            act = ActivityRepo(s).get(activity_pk)
            if act is not None:
                flags = dict(act.stage_flags or {})
                flags["intervals"] = True
                act.stage_flags = flags
            s.commit()
        ctx.incr("intervals_rows", len(rows))
        ctx.incr("intervals_activities")

    # ------------------------------------------------------------------ stage: wellness

    def sync_wellness(self, ctx: RunContext) -> None:
        """Incremental wellness from the cursor (minus overlap) to today."""
        today = self.today_local()
        with self.factory() as s:
            cursor = SyncCursorRepo(s).get(SOURCE, CURSOR_WELLNESS)
        if cursor:
            oldest = dt.date.fromisoformat(cursor[:10]) - dt.timedelta(
                days=self.options.wellness_overlap_days
            )
        else:
            oldest = today - dt.timedelta(days=self.options.initial_days)
        self._sync_wellness_window(ctx, oldest, today)
        with self.factory() as s:
            SyncCursorRepo(s).set(SOURCE, CURSOR_WELLNESS, today.isoformat())
            s.commit()

    def _sync_wellness_window(self, ctx: RunContext, oldest: dt.date, newest: dt.date) -> None:
        athlete_pk = self._athlete_id()
        raws = self.client.list_wellness(oldest, newest)
        with self.factory() as s:
            repo = WellnessRepo(s)
            for raw in raws:
                if not raw.get("id"):
                    continue
                date_local, values = mapping.wellness_row(raw)
                repo.upsert(athlete_pk, date_local, values)
                repo.mirror_fitness(athlete_pk, date_local, ctl=values["ctl"], atl=values["atl"])
                ctx.incr("wellness_days")
            s.commit()

    # ------------------------------------------------------------------ stage: power curves

    def sync_power_curves(self, ctx: RunContext) -> None:
        """Snapshot the 42d / 90d / season best-power curves as of today (+ mmp model)."""
        athlete_pk = self._athlete_id()
        today = self.today_local()
        windows = self.options.power_curve_windows
        data = self.client.get_power_curves(
            type=self.options.sport, curves=list(windows.values()), newest=today
        )
        try:
            model: JsonDict | None = self.client.get_mmp_model(self.options.sport)
        except IngestError as exc:  # the curve snapshots are still useful without the fit
            log.warning("intervals.mmp_model_failed", error=str(exc))
            model = None
        raw_list = data.get("list")
        curves: list[Any] = raw_list if isinstance(raw_list, list) else []
        by_id: dict[str, JsonDict] = {str(c.get("id")): c for c in curves if isinstance(c, dict)}
        with self.factory() as s:
            repo = PowerCurveRepo(s)
            for window, icu_curve in windows.items():
                curve = by_id.get(icu_curve)
                if curve is None:
                    ctx.incr("power_curves_missing")
                    continue
                repo.upsert(
                    athlete_pk,
                    today,
                    window,
                    mapping.power_curve_snapshot_values(curve, model),
                    source="icu",
                )
                ctx.incr("power_curves")
            s.commit()

    # ------------------------------------------------------------------ stage: events

    def sync_events(self, ctx: RunContext) -> None:
        """Mirror calendar events in ``[today - past, today + future]`` + fitness-model events."""
        today = self.today_local()
        oldest = today - dt.timedelta(days=self.options.events_past_days)
        newest = today + dt.timedelta(days=self.options.events_future_days)
        events = self.client.list_events(oldest, newest)
        try:
            fitness_events = self.client.get_fitness_model_events()
        except IngestError as exc:
            log.warning("intervals.fitness_model_events_failed", error=str(exc))
            fitness_events = []
        with self.factory() as s:
            repo = IcuEventRepo(s)
            seen: list[int] = []
            for raw in events:
                if raw.get("id") is None:
                    continue
                row = mapping.event_row(raw)
                repo.upsert(row)
                seen.append(row["id"])
                ctx.incr(
                    "events_ours" if (row["external_id"] or "").startswith("cyp:") else "events"
                )
            for raw in fitness_events:
                if raw.get("id") is None:
                    continue
                repo.upsert(mapping.event_row(raw))
                ctx.incr("fitness_model_events")
            pruned = repo.delete_missing(oldest.isoformat(), f"{newest.isoformat()}T23:59:59", seen)
            ctx.incr("events_pruned", pruned)
            s.commit()

    # ------------------------------------------------------------------ helpers

    def _backfill_pages(
        self, oldest: dt.date, newest: dt.date
    ) -> Iterator[tuple[dt.date, dt.date]]:
        """Month-sized ``(start, end)`` windows from the resume point up to ``newest``."""
        with self.factory() as s:
            progress = SyncCursorRepo(s).get(SOURCE, CURSOR_BACKFILL_PROGRESS)
        start = oldest
        if progress and "|" in progress:
            wanted_str, last_start_str = progress.split("|", 1)
            if wanted_str == oldest.isoformat():
                last_start = dt.date.fromisoformat(last_start_str[:10])
                # Resume at the last finished page (minus the edit overlap), not from scratch.
                start = max(
                    oldest, last_start - dt.timedelta(days=self.options.activity_overlap_days)
                )
        # Oldest first so a crash leaves a contiguous imported range.
        cur = start
        while cur <= newest:
            end = min(cur + dt.timedelta(days=BACKFILL_PAGE_DAYS - 1), newest)
            yield cur, end
            cur = end + dt.timedelta(days=1)


def default_stream_store(streams_dir: Path) -> StreamStore:
    """Convenience for callers that only have settings."""
    return StreamStore(streams_dir)
