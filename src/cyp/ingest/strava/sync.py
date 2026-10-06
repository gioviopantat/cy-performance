"""Strava sync job: incremental summary listing, then detail/efforts/zones (+ fallback streams).

Pipeline (port of ``strava-analyis/sync/sync.py`` onto the unified ``activities`` schema):

    athlete (+ zones) -> summary activities after cursor -> cursor advance
        -> per pending activity: detail (segments, efforts, laps in raw) -> zones
           -> streams only when ``fetch_streams`` and no Parquet exists yet (ADR-0003)
        -> efforts fallback: every ride with a ``strava_id`` whose ``stage_flags`` carry
           neither ``strava_detail`` nor ``efforts`` (icu rows whose ``detail`` was icu's, so
           the ``pending_detail`` queue never fetched Strava's segments/efforts/laps/zones),
           newest first, within the same per-run budget
        -> stream fallback: every ride with a ``strava_id`` and no ``stream_files`` row,
           whatever its ``pending_detail`` state, within the same per-run budget

Rules:
- Strava is **secondary** (ADR-0003). ``settings.strava_enabled=False`` makes :meth:`run`
  a no-op that touches neither the network nor the DB.
- The ``strava/activities_after`` cursor advances only when the listing completed; a listing
  that dies mid-pagination leaves it untouched so the next run re-fetches.
- At most ``max_detail_fetches`` reads per run across the detail queue and both fallbacks; a
  :class:`RateLimitError` (client in ``--no-wait`` mode) ends the run cleanly with everything
  so far committed.
- Negative cache: a permanent 4xx (anything but 429) on a fallback fetch is recorded in
  ``stage_flags`` as ``strava_detail_failed`` / ``strava_streams_failed`` (``"<iso ts> <status>"``)
  and the row is skipped by later runs; ``full=True`` clears both markers first.
- On an intervals.icu row the efforts fallback only fills Strava-only data (raw payload,
  segments/efforts, zones) and columns that are still ``NULL``; icu's values are never
  overwritten and icu's ``detail`` flag keeps its meaning (``strava_detail`` is separate).
- Rows are upserted by ``strava_id``; a brand-new row gets ``match_method='single_source'``
  and :mod:`cyp.ingest.matcher` merges it with its intervals.icu twin later. Landing on an icu
  row that already carries our ``strava_id`` labels it ``strava_id`` immediately.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cyp.core.activity import StreamName
from cyp.core.errors import CypError, IngestError, RateLimitError
from cyp.core.timeutil import epoch_s, iso_utc, now_utc, parse_iso
from cyp.ingest.matcher import STRAVA_SUMMARY_COLUMNS, is_icu_stub
from cyp.ingest.strava.client import StravaClient
from cyp.logging import get_logger
from cyp.settings import Settings
from cyp.store.models import Activity, Athlete, StreamFile
from cyp.store.repo.activities import ActivityRepo
from cyp.store.repo.segments import SegmentRepo
from cyp.store.repo.sync_cursors import SyncCursorRepo
from cyp.store.repo.zones import ActivityZonesRepo
from cyp.store.runs import RunContext
from cyp.store.streams import StreamStore

log = get_logger(__name__)

SOURCE = "strava"
CURSOR_AFTER = "activities_after"
CURSOR_LAST_SYNC = "last_sync_at"
CURSOR_RATE_SNAPSHOT = "rate_limit_snapshot"
DEFAULT_MAX_DETAIL_FETCHES = 60
#: ``stage_flags`` key set once Strava's detail (+efforts/laps) has been persisted for a row.
FLAG_STRAVA_DETAIL = "strava_detail"
#: Negative-cache ``stage_flags`` keys (value ``"<iso ts> <http status>"``).
FLAG_DETAIL_FAILED = "strava_detail_failed"
FLAG_STREAMS_FAILED = "strava_streams_failed"
_NEGATIVE_FLAGS = (FLAG_DETAIL_FAILED, FLAG_STREAMS_FAILED)
#: Rate-limit snapshot keys mirrored into ``sync_cursors`` for ``cyp doctor``.
_SNAPSHOT_KEYS = (
    "usage_15m",
    "limit_15m",
    "usage_daily",
    "limit_daily",
    "remaining_15m",
    "observed_at",
)

#: Strava stream key -> our Parquet column (``latlng`` is split into lat/lng).
STREAM_KEY_MAP: dict[str, str] = {
    "time": StreamName.T_S,
    "distance": StreamName.DIST_M,
    "altitude": StreamName.ALT_M,
    "velocity_smooth": StreamName.SPEED_MPS,
    "heartrate": StreamName.HR,
    "cadence": StreamName.CAD,
    "watts": StreamName.WATTS,
    "temp": StreamName.TEMP_C,
    "moving": StreamName.MOVING,
    "grade_smooth": StreamName.GRADE_PCT,
}
_INT_COLUMNS = {StreamName.HR, StreamName.CAD, StreamName.WATTS, StreamName.TEMP_C}
#: Strava ``workout_type`` values meaning "race" (1 = run race, 11 = ride race).
_RACE_WORKOUT_TYPES = {1, 11}
_TZ_RE = re.compile(r"\)\s*(\S+)\s*$")


# ------------------------------------------------------------------------------ normalisation


def _set(out: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        out[key] = value


def parse_strava_timezone(value: str | None) -> str | None:
    """``"(GMT+08:00) Asia/Taipei"`` -> ``"Asia/Taipei"``."""
    if not value:
        return None
    match = _TZ_RE.search(value)
    return match.group(1) if match else value


def _start_local(api: dict[str, Any]) -> str | None:
    raw = api.get("start_date_local")
    if not raw:
        return None
    naive = str(raw).rstrip("Z")
    offset = api.get("utc_offset")
    if offset is None:
        return naive
    total = int(offset)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{naive}{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def normalize_activity(api: dict[str, Any]) -> dict[str, Any]:
    """Map a Strava summary/detail activity onto ``activities`` columns.

    Only present, non-``None`` fields are emitted so a summary upsert after a detail upsert
    never nulls detail-only columns. ``raw_strava_json`` is always the verbatim payload.
    """
    out: dict[str, Any] = {}
    _set(out, "strava_id", api.get("id"))
    _set(out, "sport_type", api.get("sport_type") or api.get("type"))
    _set(out, "name", api.get("name"))
    _set(out, "description", api.get("description"))
    start = api.get("start_date")
    if start:
        out["start_utc"] = iso_utc(parse_iso(str(start)))
    _set(out, "start_local", _start_local(api))
    _set(out, "tz", parse_strava_timezone(api.get("timezone")))
    _set(out, "moving_s", api.get("moving_time"))
    _set(out, "elapsed_s", api.get("elapsed_time"))
    _set(out, "distance_m", api.get("distance"))
    _set(out, "elev_gain_m", api.get("total_elevation_gain"))
    for flag in ("trainer", "commute", "manual"):
        if api.get(flag) is not None:
            out[flag] = bool(api[flag])
    if api.get("workout_type") is not None:
        out["race"] = int(api["workout_type"]) in _RACE_WORKOUT_TYPES
    if api.get("device_watts") is not None:
        out["has_power"] = bool(api["device_watts"])
    if api.get("has_heartrate") is not None:
        out["has_hr"] = bool(api["has_heartrate"])
    if api.get("average_cadence") is not None:
        out["has_cadence"] = True
    _set(out, "device_name", api.get("device_name"))
    _set(out, "gear_id", api.get("gear_id"))
    gear = api.get("gear") or {}
    _set(out, "gear_name", gear.get("name"))
    _set(out, "avg_w", api.get("average_watts"))
    _set(out, "np_w", api.get("weighted_average_watts"))
    _set(out, "max_w", api.get("max_watts"))
    _set(out, "kj", api.get("kilojoules"))
    _set(out, "avg_hr", api.get("average_heartrate"))
    _set(out, "max_hr", api.get("max_heartrate"))
    _set(out, "avg_cad", api.get("average_cadence"))
    out["raw_strava_json"] = api
    return out


def normalize_segment(api: dict[str, Any]) -> dict[str, Any]:
    """Map a Strava segment onto ``segments`` columns."""
    out: dict[str, Any] = {"id": api["id"]}
    _set(out, "name", api.get("name"))
    _set(out, "activity_type", api.get("activity_type"))
    _set(out, "distance_m", api.get("distance"))
    for key in (
        "average_grade",
        "maximum_grade",
        "elevation_high",
        "elevation_low",
        "climb_category",
        "city",
        "state",
        "country",
    ):
        _set(out, key, api.get(key))
    if api.get("starred") is not None:
        out["starred"] = bool(api["starred"])
    out["raw_json"] = api
    return out


def normalize_segment_effort(api: dict[str, Any], *, activity_id: int) -> dict[str, Any]:
    """Map a Strava segment effort onto ``segment_efforts`` (``activity_id`` is our internal id)."""
    out: dict[str, Any] = {"id": api["id"], "activity_id": activity_id}
    _set(out, "segment_id", (api.get("segment") or {}).get("id"))
    _set(out, "name", api.get("name"))
    if api.get("start_date"):
        out["start_utc"] = iso_utc(parse_iso(str(api["start_date"])))
    _set(out, "start_index", api.get("start_index"))
    _set(out, "end_index", api.get("end_index"))
    _set(out, "elapsed_s", api.get("elapsed_time"))
    _set(out, "moving_s", api.get("moving_time"))
    _set(out, "distance_m", api.get("distance"))
    _set(out, "avg_w", api.get("average_watts"))
    if api.get("device_watts") is not None:
        out["device_watts"] = bool(api["device_watts"])
    _set(out, "avg_hr", api.get("average_heartrate"))
    _set(out, "max_hr", api.get("max_heartrate"))
    _set(out, "avg_cad", api.get("average_cadence"))
    _set(out, "pr_rank", api.get("pr_rank"))
    _set(out, "kom_rank", api.get("kom_rank"))
    _set(out, "achievements", api.get("achievements"))
    out["raw_json"] = api
    return out


def normalize_zone(api: dict[str, Any]) -> dict[str, Any]:
    """Map one entry of ``GET /activities/{id}/zones`` onto ``activity_zones`` columns."""
    out: dict[str, Any] = {}
    if api.get("sensor_based") is not None:
        out["sensor_based"] = bool(api["sensor_based"])
    if api.get("custom_zones") is not None:
        out["custom_zones"] = bool(api["custom_zones"])
    points = api.get("points", api.get("score"))
    if points is not None:
        out["points"] = int(points)
    _set(out, "distribution_buckets", api.get("distribution_buckets"))
    out["raw_json"] = api
    return out


def streams_to_frame(streams: dict[str, Any]) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Convert a ``key_by_type`` streams payload into a 1 Hz frame with our column names.

    Returns ``(frame, meta)`` where ``meta`` has ``original_size``, ``resolution`` and
    ``resampled`` (True when the ``time`` stream had gaps and rows were forward-filled).

    Raises:
        IngestError: no usable ``time`` stream.
    """
    time_stream = streams.get("time") or {}
    t_raw = time_stream.get("data")
    if not t_raw:
        raise IngestError("Strava streams payload has no 'time' data")
    n = len(t_raw)
    columns: dict[str, list[Any]] = {StreamName.T_S: [int(t) for t in t_raw]}
    for key, col in STREAM_KEY_MAP.items():
        if key == "time":
            continue
        data = (streams.get(key) or {}).get("data")
        if data is None or len(data) != n:
            continue
        if col in _INT_COLUMNS:
            data = [None if v is None else round(float(v)) for v in data]
        columns[col] = list(data)
    latlng = (streams.get("latlng") or {}).get("data")
    if latlng is not None and len(latlng) == n:
        columns[StreamName.LAT] = [None if p is None else p[0] for p in latlng]
        columns[StreamName.LNG] = [None if p is None else p[1] for p in latlng]
    frame = (
        pl.DataFrame(columns, strict=False)
        .sort(StreamName.T_S)
        .unique(subset=[StreamName.T_S], keep="first", maintain_order=True)
    )
    t_max = max(columns[StreamName.T_S])
    t_min = min(columns[StreamName.T_S])
    resampled = False
    if frame.height != t_max + 1 or t_min != 0:
        full = pl.DataFrame({StreamName.T_S: pl.int_range(0, t_max + 1, eager=True)})
        frame = full.join(frame, on=StreamName.T_S, how="left").fill_null(strategy="forward")
        resampled = True
    meta = {
        "original_size": time_stream.get("original_size", n),
        "resolution": time_stream.get("resolution"),
        "resampled": resampled,
    }
    return frame, meta


# -------------------------------------------------------------------------------------- syncer


@dataclass
class StravaSyncSummary:
    """Counts and outcome of one :meth:`StravaSyncer.run`."""

    skipped: bool = False
    athlete_id: int | None = None
    activities_seen: int = 0
    activities_new: int = 0
    details_fetched: int = 0
    segments: int = 0
    efforts: int = 0
    laps: int = 0
    zones_fetched: int = 0
    streams_fetched: int = 0
    streams_skipped_existing: int = 0
    streams_fallback_fetched: int = 0
    streams_fallback_remaining: int = 0
    efforts_fallback_fetched: int = 0
    efforts_fallback_remaining: int = 0
    fetch_failures_cached: int = 0
    negative_cache_cleared: int = 0
    activities_joined_icu: int = 0
    details_remaining: int = 0
    rate_limited: bool = False
    cursor_before: int | None = None
    cursor_after: int | None = None
    errors: list[str] = field(default_factory=list)
    rate_limit: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        """Integer counters for ``job_runs.counts`` (ids and cursors excluded)."""
        skip = {"athlete_id", "cursor_before", "cursor_after"}
        return {
            k: v
            for k, v in asdict(self).items()
            if k not in skip and isinstance(v, int) and not isinstance(v, bool)
        } | {"errors": len(self.errors)}


class StravaSyncer:
    """Pull Strava data into the store; see the module docstring for the pipeline."""

    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        client: StravaClient,
        stream_store: StreamStore | None = None,
        *,
        max_detail_fetches: int = DEFAULT_MAX_DETAIL_FETCHES,
        fetch_streams: bool = False,
        sync_athlete: bool = True,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.client = client
        self.stream_store = stream_store or StreamStore(settings.streams_dir)
        self.max_detail_fetches = max_detail_fetches
        self.fetch_streams = fetch_streams
        self.sync_athlete = sync_athlete

    # -- public ----------------------------------------------------------------------------
    def run(self, ctx: RunContext | None = None, *, full: bool = False) -> StravaSyncSummary:
        """Execute one sync. ``full=True`` ignores the cursor (re-list everything)."""
        summary = StravaSyncSummary()
        if not self.settings.strava_enabled:
            summary.skipped = True
            log.info("strava.sync.skipped", reason="STRAVA_ENABLED=false")
            return summary
        try:
            if full:
                self._clear_negative_cache(summary)
            self._sync_athlete(summary)
            self._sync_listing(summary, full=full)
            self._sync_details(summary)
            self._sync_efforts_fallback(summary)
            self._sync_stream_fallback(summary)
        except RateLimitError as exc:
            summary.rate_limited = True
            summary.errors.append(f"rate_limit: {exc}")
            log.warning("strava.sync.rate_limited", retry_after_s=exc.retry_after_s)
        finally:
            self._finish(summary, ctx)
        return summary

    # -- stages ----------------------------------------------------------------------------
    def _sync_athlete(self, summary: StravaSyncSummary) -> None:
        if not self.sync_athlete:
            return
        try:
            athlete = self.client.get_athlete()
        except IngestError as exc:
            summary.errors.append(f"athlete: {exc}")
            return
        if athlete.get("id") is None:
            return
        zones: dict[str, Any] | None = None
        try:
            zones = self.client.get_zones()
        except IngestError as exc:
            summary.errors.append(f"athlete_zones: {exc}")
        with self.factory() as session:
            row = session.scalars(
                select(Athlete).where(Athlete.strava_id == int(athlete["id"]))
            ).first()
            if row is None:
                row = session.scalars(select(Athlete).order_by(Athlete.id).limit(1)).first()
            if row is None:
                row = Athlete()
                session.add(row)
            row.strava_id = int(athlete["id"])
            name = " ".join(p for p in (athlete.get("firstname"), athlete.get("lastname")) if p)
            if name:
                row.name = name
            if athlete.get("sex"):
                row.sex = athlete["sex"]
            if athlete.get("weight"):
                row.weight_kg = float(athlete["weight"])
            raw = dict(athlete)
            if zones is not None:
                raw["zones"] = zones
            row.raw_strava_json = raw
            row.updated_at = iso_utc(now_utc())
            session.commit()
            summary.athlete_id = row.id

    def _sync_listing(self, summary: StravaSyncSummary, *, full: bool) -> None:
        with self.factory() as session:
            cursors = SyncCursorRepo(session)
            raw = cursors.get(SOURCE, CURSOR_AFTER)
        after = int(raw) if raw and raw.isdigit() and not full else None
        summary.cursor_before = after
        max_epoch = after or 0
        try:
            for api in self.client.iter_activities(after=after):
                if api.get("id") is None:
                    continue
                with self.factory() as session:
                    self._upsert_summary(session, api, summary)
                    session.commit()
                start = api.get("start_date")
                if start:
                    max_epoch = max(max_epoch, epoch_s(parse_iso(str(start))))
        except RateLimitError:
            raise
        except CypError as exc:
            summary.errors.append(f"list_activities: {exc}")
            log.warning("strava.sync.listing_failed", error=str(exc))
            return
        if max_epoch and max_epoch != after:
            with self.factory() as session:
                SyncCursorRepo(session).set(SOURCE, CURSOR_AFTER, str(max_epoch))
                session.commit()
        summary.cursor_after = max_epoch or after

    def _upsert_summary(
        self, session: Session, api: dict[str, Any], summary: StravaSyncSummary
    ) -> Activity:
        repo = ActivityRepo(session)
        values = normalize_activity(api)
        existing = repo.get_by_strava_id(int(api["id"]))
        if existing is None:
            values["match_method"] = "single_source"
            if summary.athlete_id is not None:
                values["athlete_id"] = summary.athlete_id
            summary.activities_new += 1
        elif existing.intervals_id is not None and existing.match_method == "single_source":
            values["match_method"] = "strava_id"  # icu row carried our strava_id (docs/02 §3.6)
            summary.activities_joined_icu += 1
        summary.activities_seen += 1
        return repo.upsert(values)

    def _pending_detail_ids(self) -> list[int]:
        with self.factory() as session:
            stmt = (
                select(Activity.id)
                .where(Activity.pending_detail.is_(True), Activity.strava_id.is_not(None))
                .order_by(Activity.start_utc)
            )
            return list(session.scalars(stmt))

    def _sync_details(self, summary: StravaSyncSummary) -> None:
        ids = self._pending_detail_ids()
        budget = ids[: self.max_detail_fetches]
        summary.details_remaining = len(ids)
        for activity_id in budget:
            try:
                self._sync_one(activity_id, summary)
            except RateLimitError:
                raise  # ``details_remaining`` still counts this one: nothing was committed
            except CypError as exc:
                summary.errors.append(f"activity {activity_id}: {exc}")
                log.warning("strava.sync.activity_failed", activity_id=activity_id, error=str(exc))
            summary.details_remaining -= 1

    def _sync_one(self, activity_id: int, summary: StravaSyncSummary) -> None:
        with self.factory() as session:
            repo = ActivityRepo(session)
            row = repo.get(activity_id)
            if row is None or row.strava_id is None:
                return
            strava_id = row.strava_id
            detail = self.client.get_activity(strava_id, include_all_efforts=True)
            summary.details_fetched += 1
            values = normalize_activity(detail)
            values.pop("match_method", None)
            row = repo.upsert(values)
            self._persist_efforts(session, row, detail, summary)
            summary.laps += len(detail.get("laps") or [])
            repo.mark_done(row, "detail")
            _set_flag(row, "efforts")
            _set_flag(row, FLAG_STRAVA_DETAIL)
            session.commit()

            self._sync_zones(session, row, strava_id, summary)
            session.commit()

            if self.fetch_streams:
                self._sync_streams(session, row, strava_id, summary)
                session.commit()

    def _reads_left(self, summary: StravaSyncSummary) -> int:
        """Budget left for this run: ``max_detail_fetches`` shared by every detail-sized stage."""
        used = (
            summary.details_fetched
            + summary.efforts_fallback_fetched
            + summary.streams_fallback_fetched
        )
        return max(self.max_detail_fetches - used, 0)

    def _fallback_detail_ids(self) -> list[int]:
        """Rides with a Strava id whose Strava detail was never fetched, newest first.

        Targets icu rows: icu marks ``detail`` done and clears ``pending_detail`` itself, so
        the detail queue never asks Strava for their segments/efforts/laps/zones. Rows carrying
        a ``strava_detail_failed`` negative-cache marker are skipped.
        """
        with self.factory() as session:
            stmt = (
                select(Activity.id, Activity.stage_flags)
                .where(Activity.strava_id.is_not(None), Activity.is_ride.is_(True))
                .order_by(Activity.start_utc.desc())
            )
            return [
                activity_id
                for activity_id, flags in session.execute(stmt)
                if not _has_any_flag(flags, FLAG_STRAVA_DETAIL, "efforts", FLAG_DETAIL_FAILED)
            ]

    def _sync_efforts_fallback(self, summary: StravaSyncSummary) -> None:
        """Stage 3b: Strava detail (+efforts, laps, zones) for rides icu "detailed" first.

        Shares the per-run budget with the detail queue and the stream fallback. A permanent
        4xx is negative-cached in ``stage_flags`` so the row is not retried every run.
        """
        ids = self._fallback_detail_ids()
        summary.efforts_fallback_remaining = len(ids)
        for activity_id in ids[: self._reads_left(summary)]:
            try:
                self._sync_one_fallback(activity_id, summary)
            except RateLimitError:
                raise  # nothing committed for this one: ``efforts_fallback_remaining`` keeps it
            except CypError as exc:
                summary.errors.append(f"efforts_fallback {activity_id}: {exc}")
                log.warning(
                    "strava.sync.efforts_fallback_failed", activity_id=activity_id, error=str(exc)
                )
            summary.efforts_fallback_remaining -= 1

    def _sync_one_fallback(self, activity_id: int, summary: StravaSyncSummary) -> None:
        with self.factory() as session:
            repo = ActivityRepo(session)
            row = repo.get(activity_id)
            if row is None or row.strava_id is None:
                return
            strava_id = row.strava_id
            try:
                detail = self.client.get_activity(strava_id, include_all_efforts=True)
            except IngestError as exc:
                if _is_permanent(exc):
                    _set_failed(row, FLAG_DETAIL_FAILED, exc)
                    summary.fetch_failures_cached += 1
                    session.commit()
                raise
            summary.efforts_fallback_fetched += 1
            self._merge_strava_detail(repo, row, detail)
            self._persist_efforts(session, row, detail, summary)
            summary.laps += len(detail.get("laps") or [])
            _set_flag(row, "efforts")
            _set_flag(row, FLAG_STRAVA_DETAIL)
            row.pending_analysis = True  # segments/efforts feed the analyzer
            session.commit()
            self._sync_zones(session, row, strava_id, summary)
            session.commit()

    @staticmethod
    def _merge_strava_detail(repo: ActivityRepo, row: Activity, detail: dict[str, Any]) -> None:
        """Apply a Strava detail payload without clobbering intervals.icu's columns.

        A Strava-only row takes the whole normalised payload (as the detail queue does). An
        icu row keeps every non-``NULL`` column and only gains the raw payload plus columns
        icu left empty; an icu *stub* (Strava-origin activity hidden by icu) takes Strava's
        values outright, mirroring :mod:`cyp.ingest.matcher`.
        """
        values = normalize_activity(detail)
        values.pop("match_method", None)
        if row.intervals_id is None:
            repo.upsert(values)
            return
        stub = is_icu_stub(row)
        row.raw_strava_json = values.pop("raw_strava_json")
        for col, value in values.items():
            if col not in STRAVA_SUMMARY_COLUMNS:
                continue
            if stub or getattr(row, col) is None:
                setattr(row, col, value)
        row.updated_at = iso_utc(now_utc())
        repo.flush()

    def _sync_zones(
        self, session: Session, row: Activity, strava_id: int, summary: StravaSyncSummary
    ) -> None:
        try:
            zones = self.client.get_activity_zones(strava_id)
        except RateLimitError as exc:
            # Leave ``zones`` unflagged so a later run can still fetch it; the next read
            # raises again at the client's budget check and ends the run cleanly.
            summary.errors.append(f"zones {strava_id}: {exc}")
            return
        except IngestError as exc:
            summary.errors.append(f"zones {strava_id}: {exc}")
        else:
            zrepo = ActivityZonesRepo(session)
            for z in zones:
                if z.get("type"):
                    zrepo.upsert(row.id, str(z["type"]), normalize_zone(z))
            summary.zones_fetched += 1
        _set_flag(row, "zones")

    def _fallback_stream_ids(self) -> list[int]:
        """Rides with a Strava id and no Parquet yet, newest first (regardless of detail state).

        Covers icu rows whose streams icu cannot serve (Strava-origin activities, empty
        payloads) as well as detail-complete Strava rows synced before ``--streams`` was on.
        Rows carrying a ``strava_streams_failed`` negative-cache marker are skipped.
        """
        with self.factory() as session:
            stmt = (
                select(Activity.id, Activity.stage_flags)
                .outerjoin(StreamFile, StreamFile.activity_id == Activity.id)
                .where(
                    Activity.strava_id.is_not(None),
                    Activity.is_ride.is_(True),
                    StreamFile.activity_id.is_(None),
                )
                .order_by(Activity.start_utc.desc())
            )
            return [
                activity_id
                for activity_id, flags in session.execute(stmt)
                if not _has_any_flag(flags, FLAG_STREAMS_FAILED)
            ]

    def _sync_stream_fallback(self, summary: StravaSyncSummary) -> None:
        """Stage 4 (ADR-0003 §2): Strava streams for rides nobody else has streams for.

        Shares the per-run budget with the detail queue and the efforts fallback.
        """
        if not self.fetch_streams:
            return
        ids = self._fallback_stream_ids()
        summary.streams_fallback_remaining = len(ids)
        for activity_id in ids[: self._reads_left(summary)]:
            before = summary.streams_fetched
            with self.factory() as session:
                row = ActivityRepo(session).get(activity_id)
                if row is None or row.strava_id is None:
                    continue
                self._sync_streams(session, row, row.strava_id, summary)
                session.commit()
            summary.streams_fallback_fetched += summary.streams_fetched - before
            summary.streams_fallback_remaining -= 1

    def _clear_negative_cache(self, summary: StravaSyncSummary) -> None:
        """``--full``: forget every ``*_failed`` marker so the fallbacks retry those rows."""
        with self.factory() as session:
            stmt = select(Activity).where(Activity.stage_flags.is_not(None))
            for row in session.scalars(stmt):
                flags = row.stage_flags or {}
                if not any(name in flags for name in _NEGATIVE_FLAGS):
                    continue
                row.stage_flags = {k: v for k, v in flags.items() if k not in _NEGATIVE_FLAGS}
                summary.negative_cache_cleared += 1
            session.commit()

    def _persist_efforts(
        self,
        session: Session,
        row: Activity,
        detail: dict[str, Any],
        summary: StravaSyncSummary,
    ) -> None:
        srepo = SegmentRepo(session)
        for effort in detail.get("segment_efforts") or []:
            segment = effort.get("segment") or {}
            if segment.get("id") is None or effort.get("id") is None:
                continue
            srepo.upsert_segment(normalize_segment(segment))
            summary.segments += 1
            srepo.upsert_effort(normalize_segment_effort(effort, activity_id=row.id))
            summary.efforts += 1

    def _sync_streams(
        self, session: Session, row: Activity, strava_id: int, summary: StravaSyncSummary
    ) -> None:
        if self.stream_store.exists(row.id) or session.get(StreamFile, row.id) is not None:
            summary.streams_skipped_existing += 1
            return
        try:
            payload = self.client.get_streams(strava_id)
            frame, meta = streams_to_frame(payload)
        except RateLimitError:
            raise  # ends the run cleanly; everything committed so far stays
        except IngestError as exc:
            summary.errors.append(f"streams {strava_id}: {exc}")
            if _is_permanent(exc):
                _set_failed(row, FLAG_STREAMS_FAILED, exc)
                summary.fetch_failures_cached += 1
            return
        path = self.stream_store.write(row.id, frame)
        raw_path: str | None = None
        if meta["resampled"]:
            raw_file = path.with_suffix(".strava.json")
            raw_file.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            raw_path = str(raw_file)
        session.merge(
            StreamFile(
                activity_id=row.id,
                source=SOURCE,
                path=str(path),
                columns=list(frame.columns),
                hz=1.0,
                original_size=meta["original_size"],
                resolution=meta["resolution"],
                n_samples=frame.height,
                raw_json_path=raw_path,
            )
        )
        ActivityRepo(session).mark_done(row, "streams")
        summary.streams_fetched += 1

    def _finish(self, summary: StravaSyncSummary, ctx: RunContext | None) -> None:
        summary.rate_limit = self.client.rate_limit.snapshot()
        with self.factory() as session:
            cursors = SyncCursorRepo(session)
            cursors.set(SOURCE, CURSOR_LAST_SYNC, iso_utc(now_utc()))
            # ``sync_cursors.value`` is String(200): keep the doctor-facing subset only.
            compact = {k: summary.rate_limit.get(k) for k in _SNAPSHOT_KEYS}
            cursors.set(SOURCE, CURSOR_RATE_SNAPSHOT, json.dumps(compact, separators=(",", ":")))
            session.commit()
        if ctx is not None:
            for key, value in summary.counts().items():
                ctx.incr(key, value)
            ctx.rate_limit_snapshot = summary.rate_limit
        log.info("strava.sync.done", **summary.counts(), rate_limited=summary.rate_limited)


def _set_flag(row: Activity, name: str) -> None:
    flags = dict(row.stage_flags or {})
    flags[name] = True
    row.stage_flags = flags


def _set_failed(row: Activity, name: str, exc: IngestError) -> None:
    """Negative-cache marker: ``"<iso ts> <status>"`` under ``name``."""
    flags = dict(row.stage_flags or {})
    flags[name] = f"{iso_utc(now_utc())} {exc.status}"
    row.stage_flags = flags


def _has_any_flag(flags: Any, *names: str) -> bool:
    return isinstance(flags, dict) and any(flags.get(n) for n in names)


def _is_permanent(exc: IngestError) -> bool:
    """A 4xx other than 429 will not succeed on retry (404 gone, 403 private, 402 …)."""
    return exc.status is not None and 400 <= exc.status < 500 and exc.status != 429
