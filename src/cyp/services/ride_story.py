"""One ride's casual Strava RIDE.LOG: route lookup, narration, cache (spec casual-and-strava C).

:func:`story` is what every display uses (web, CLI, Strava push): the cached story, else a
deterministic one from the ride report, never a network call. :func:`prepare` is the only
function that calls out: OpenStreetMap with flag ``strava.route_story`` and Claude with flag
``llm.ride_poem`` (ADR-0021); any failure degrades to the deterministic text. Files, all in the
profile's data dir: ``ride_story/<id>.json`` (the story), ``ride_story/osm/<id>.json`` (the
route lookup), ``ride_story/visited.json`` (every place a route passed, for next-spot tips).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from cyp.core.errors import AnalysisError, CypError, NotFoundError
from cyp.ingest.osm import privacy
from cyp.ingest.osm.client import AROUND_M, KINDS, SPOT_KINDS, OsmClient, Place
from cyp.llm.narrator import ClaudeNarrator, Narrator
from cyp.logging import get_logger
from cyp.reports import ride_story as copy
from cyp.reports.ride_model import RideReport
from cyp.services import ride_feedback
from cyp.services.context import AppContext
from cyp.store.models import Activity

log = get_logger(__name__)

ROUTE_FLAG = "strava.route_story"
POEM_FLAG = "llm.ride_poem"
#: Earlier stories read for variety (previous type, recent heads).
HISTORY = 30
SPOT_WINDOW_DAYS = 90
SPOT_MIN_M, SPOT_MAX_M = 3_000.0, 40_000.0
STOP_RADIUS_M = 150.0
BBOX_PAD_DEG = 0.02
#: Margin of the route box (about 200 m), so places just beside the route are found.
AROUND_PAD_DEG = 0.002
#: A second narration call is made only when the first broke a rule (it gets the problems).
NARRATION_ATTEMPTS = 2

OsmFactory = Callable[[], OsmClient]


@dataclass(frozen=True)
class Story:
    """One ride's RIDE.LOG content (the cache format)."""

    activity_id: int
    kind: str
    head: str
    lines: list[str]
    poem: list[str] = field(default_factory=list)
    #: ``deterministic`` | ``route`` (with OSM facts) | ``llm`` (narrated)
    source: str = "deterministic"
    #: Made by :func:`prepare` with these flags; reused as is while they stay the same.
    prepared: bool = False
    flags: dict[str, bool] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """The RIDE.LOG block."""
        return copy.compose_block(self.lines, self.poem)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {"version": copy.STORY_VERSION, "activity_id": self.activity_id,
                "kind": self.kind, "head": self.head, "lines": self.lines, "poem": self.poem,
                "source": self.source, "prepared": self.prepared, "flags": self.flags,
                "facts": self.facts}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Story:
        """Inverse of :meth:`to_dict`."""
        return cls(
            activity_id=int(d["activity_id"]),
            kind=str(d["kind"]),
            head=str(d["head"]),
            lines=[str(x) for x in d.get("lines") or []],
            poem=[str(x) for x in d.get("poem") or []],
            source=str(d.get("source", "deterministic")),
            prepared=bool(d.get("prepared")),
            flags={str(k): bool(v) for k, v in (d.get("flags") or {}).items()},
            facts=dict(d.get("facts") or {}),
        )


# ----------------------------------------------------------------------------- files


def story_dir(ctx: AppContext) -> Path:
    """``data/ride_story``."""
    return ctx.settings.cyp_data_dir / "ride_story"


def _story_path(ctx: AppContext, activity_id: int) -> Path:
    return story_dir(ctx) / f"{activity_id}.json"


def _osm_path(ctx: AppContext, activity_id: int) -> Path:
    return story_dir(ctx) / "osm" / f"{activity_id}.json"


def _visited_path(ctx: AppContext) -> Path:
    return story_dir(ctx) / "visited.json"


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def needs_retry(ctx: AppContext, activity_id: int, now: dt.datetime, every: dt.timedelta) -> bool:
    """A cached story whose lookup or narration failed, last tried more than ``every`` ago."""
    cached = load_cached(ctx, activity_id)
    if cached is None or cached.prepared:
        return False
    tried = dt.datetime.fromtimestamp(_story_path(ctx, activity_id).stat().st_mtime, dt.UTC)
    return now - tried >= every


def load_cached(ctx: AppContext, activity_id: int) -> Story | None:
    """The cached story of a ride (``None``: none, or an older format)."""
    data = _read(_story_path(ctx, activity_id))
    if data is None or data.get("version") != copy.STORY_VERSION:
        return None
    try:
        return Story.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _flags(ctx: AppContext) -> dict[str, bool]:
    f = ctx.features()
    return {"route": f.enabled(ROUTE_FLAG), "poem": f.enabled(POEM_FLAG)}


# ----------------------------------------------------------------------------- inputs


def _report(ctx: AppContext, activity_id: int) -> RideReport:
    """The ride report the PDF used (its facts JSON), else built the same way (no tomorrow)."""
    from cyp.services import ride_report

    folder = ride_report.ride_dir(ctx)
    stored = sorted(folder.glob(f"*_{activity_id}.json")) if folder.is_dir() else []
    if stored:
        try:
            return ride_report.load_report(stored[-1].with_suffix(".pdf"))
        except (ValueError, OSError):
            pass
    from cyp.reports.ride_facts import build_ride_report

    fb = ride_feedback.get(ctx, activity_id)
    with ctx.factory() as s:
        return build_ride_report(
            s,
            ctx.store,
            activity_id,
            profile=ctx.settings.cyp_profile or "default",
            cfg=ctx.athlete_config_or_none(),
            feedback=(fb.rpe, fb.feel),
            casual=fb.casual,
        )


def _history(ctx: AppContext, activity_id: int) -> tuple[str | None, list[str]]:
    """The previous ride's content type and the recently used acrostic heads."""
    with ctx.factory() as s:
        a = s.get(Activity, activity_id)
        if a is None:
            raise NotFoundError(f"no activity {activity_id}")
        start = a.start_local or a.start_utc
        earlier = s.scalars(
            select(Activity.id)
            .where(Activity.is_ride.is_(True), Activity.start_local < start)
            .order_by(Activity.start_local.desc())
            .limit(HISTORY)
        ).all()
    previous: str | None = None
    heads: list[str] = []
    for i in earlier:
        cached = load_cached(ctx, int(i))
        if cached is None:
            continue
        previous = previous or cached.kind
        if len(heads) < copy.RECENT_HEADS:
            heads.append(cached.head)
    return previous, heads


def _route_cached(ctx: AppContext, activity_id: int) -> copy.RouteInfo | None:
    data = _read(_osm_path(ctx, activity_id))
    if data is None or not isinstance(data.get("route"), dict):
        return None
    return copy.RouteInfo.from_dict(data["route"])


# ------------------------------------------------------------------------------ route


def _points(ctx: AppContext, activity_id: int) -> list[privacy.Point]:
    from cyp.analysis.ride.frames import load_frame

    with ctx.factory() as s:
        a = s.get(Activity, activity_id)
        trainer = bool(a.trainer) if a is not None else False
    if trainer:
        return []
    try:
        frame = load_frame(ctx.store, activity_id, trainer=False)
    except (NotFoundError, AnalysisError):
        return []
    if frame.lat is None or frame.lng is None:
        return []
    return privacy.valid_points(frame.lat.tolist(), frame.lng.tolist())


def _stop_point(ctx: AppContext, activity_id: int, report: RideReport) -> privacy.Point | None:
    """Where the longest stop was (the stream's position at its start)."""
    from cyp.analysis.ride.frames import load_frame

    o = report.outdoor
    if o is None or not o.stops:
        return None
    longest = max(o.stops, key=lambda seg: seg.end_s - seg.start_s)
    try:
        frame = load_frame(ctx.store, activity_id, trainer=False)
    except (NotFoundError, AnalysisError):
        return None
    if frame.lat is None or frame.lng is None or frame.n == 0:
        return None
    i = min(max(longest.start_s, 0), frame.n - 1)
    pts = privacy.valid_points([float(frame.lat[i])], [float(frame.lng[i])])
    return pts[0] if pts else None


def _visited(ctx: AppContext) -> dict[str, str]:
    data = _read(_visited_path(ctx)) or {}
    return {str(k): str(v) for k, v in data.items()}


def _usual_box(
    ctx: AppContext, day: dt.date, own: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """The cached (privacy-trimmed) routes of the last 90 days plus this one, padded."""
    boxes = [own]
    folder = story_dir(ctx) / "osm"
    for f in folder.glob("*.json") if folder.is_dir() else []:
        data = _read(f)
        if not data or not data.get("bbox") or not data.get("day"):
            continue
        try:
            when = dt.date.fromisoformat(str(data["day"]))
        except ValueError:
            continue
        if day - dt.timedelta(days=SPOT_WINDOW_DAYS) <= when <= day:
            boxes.append(tuple(data["bbox"]))  # type: ignore[arg-type]
    s, w, n, e = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes))  # fmt: skip
    return (s - BBOX_PAD_DEG, w - BBOX_PAD_DEG, n + BBOX_PAD_DEG, e + BBOX_PAD_DEG)


def _padded(box: tuple[float, float, float, float] | None) -> tuple[float, float, float, float]:
    """``box`` with :data:`AROUND_PAD_DEG` on every side (places just off the route)."""
    assert box is not None
    s, w, n, e = box
    return (s - AROUND_PAD_DEG, w - AROUND_PAD_DEG, n + AROUND_PAD_DEG, e + AROUND_PAD_DEG)


def _distance_to(points: list[privacy.Point], p: privacy.Point) -> float:
    """Metres from ``p`` to the nearest of ``points``."""
    return min((privacy.haversine_m(q, p) for q in points), default=float("inf"))


def _safe_box(
    box: tuple[float, float, float, float], start: privacy.Point, end: privacy.Point
) -> tuple[float, float, float, float]:
    """``box`` grown until none of its corners is inside the privacy zone."""
    s, w, n, e = box
    for _ in range(20):
        corners = ((s, w), (s, e), (n, w), (n, e))
        if all(privacy.outside_zone(c, start, end) for c in corners):
            break
        s, w, n, e = s - BBOX_PAD_DEG, w - BBOX_PAD_DEG, n + BBOX_PAD_DEG, e + BBOX_PAD_DEG
    return (round(s, 4), round(w, 4), round(n, 4), round(e, 4))


def route_info(
    ctx: AppContext, activity_id: int, report: RideReport, client: OsmClient
) -> copy.RouteInfo:
    """Look the route up on OSM (cached per ride); only privacy-trimmed points leave.

    Raises:
        OsmError: a request failed (nothing is cached then).
    """
    cached = _route_cached(ctx, activity_id)
    if cached is not None:
        return cached
    points = _points(ctx, activity_id)
    ends = privacy.endpoints(points)
    trimmed = privacy.trimmed_route(points)
    if ends is None or not trimmed:
        info = copy.RouteInfo()
        _write(_osm_path(ctx, activity_id), {"day": None, "bbox": None, "route": info.to_dict()})
        return info
    start, end = ends

    def kept(p: Place) -> bool:  # a place next to home would give home away
        return privacy.outside_zone((p.lat, p.lng), start, end)

    def position(p: Place) -> int:
        return min(
            range(len(trimmed)), key=lambda i: privacy.haversine_m(trimmed[i], (p.lat, p.lng))
        )

    # Only a box leaves (corners outside the zone); "on the route" is decided here.
    local = points[:: max(1, len(points) // 2000)]
    along = client.places_in(_safe_box(_padded(privacy.bbox(trimmed)), start, end), tuple(KINDS))
    places = sorted(
        (p for p in along if kept(p) and _distance_to(local, (p.lat, p.lng)) <= AROUND_M),
        key=position,
    )
    turn = max(trimmed, key=lambda p: privacy.haversine_m(p, start))
    stop: Place | None = None
    sp = _stop_point(ctx, activity_id, report)
    if sp is not None and privacy.outside_zone(sp, start, end):
        drinks = [p for p in places if p.kind in ("cafe", "drink")]
        near = min(drinks, key=lambda p: privacy.haversine_m(sp, (p.lat, p.lng)), default=None)
        if near is not None and privacy.haversine_m(sp, (near.lat, near.lng)) <= STOP_RADIUS_M:
            stop = near
    own = privacy.bbox(trimmed)
    assert own is not None
    day = report.ride.start_local.date()
    visited = _visited(ctx)
    passed = {p.osm_id for p in places}
    # A chain's other branch is not a new place: names already seen are skipped too.
    seen_names = set(visited.values()) | {p.name for p in places}
    candidates = [
        p
        for p in client.places_in(_safe_box(_usual_box(ctx, day, own), start, end))
        if p.kind in SPOT_KINDS
        and p.osm_id not in visited
        and p.osm_id not in passed
        and p.name not in seen_names
        and kept(p)
        and SPOT_MIN_M <= privacy.haversine_m(turn, (p.lat, p.lng)) <= SPOT_MAX_M
    ]
    best = next((k for k in SPOT_KINDS if any(p.kind == k for p in candidates)), None)
    pool = sorted((p for p in candidates if p.kind == best), key=lambda p: p.osm_id)
    spot = pool[activity_id % len(pool)] if pool else None
    spot_km = round(privacy.haversine_m(turn, (spot.lat, spot.lng)) / 1000) if spot else None
    spot_dir = copy.compass(turn, (spot.lat, spot.lng)) if spot else None
    info = copy.RouteInfo(places, stop, spot, spot_km, spot_dir)
    _write(
        _osm_path(ctx, activity_id),
        {
            "day": day.isoformat(),
            "bbox": list(own),
            "sent_points": len(trimmed),
            "route": info.to_dict(),
        },
    )
    visited.update({p.osm_id: p.name for p in places})
    _write(_visited_path(ctx), visited)
    return info


# ------------------------------------------------------------------------------ story


def _compose(
    ctx: AppContext,
    activity_id: int,
    route: copy.RouteInfo | None,
    narrator: Narrator | None,
    report: RideReport | None = None,
) -> tuple[Story, bool]:
    """The story and whether every requested step worked (``False``: retry next prepare)."""
    report = report if report is not None else _report(ctx, activity_id)
    casual = ride_feedback.get(ctx, activity_id).casual
    cfg = ctx.athlete_config_or_none()
    named = cfg.location.climbs if cfg is not None else []
    facts = copy.story_facts(report, route, casual=casual, named=named)
    previous, heads = _history(ctx, activity_id)
    kind = copy.pick_type(facts, previous)
    head = copy.pick_head(kind, activity_id, heads)
    lines = copy.deterministic_lines(facts, kind)
    poem: list[str] = []
    source = "route" if route is not None and (route.stop or route.spot) else "deterministic"
    ok = True
    payload = {**facts.to_dict(), "content_type": kind, "draft_lines": lines}
    problems: list[str] = []
    for attempt in range(NARRATION_ATTEMPTS if narrator is not None and lines else 0):
        assert narrator is not None
        try:
            got = narrator(payload, head, problems)
        except CypError as exc:
            log.warning("ride_story.narrator_failed", activity_id=activity_id, error=str(exc))
            ok = False
            break
        if got is None:
            log.warning("ride_story.narrator_declined", activity_id=activity_id)
            break
        problems = copy.validate(got.lines, got.poem, facts, head, draft=lines)
        if problems:
            log.warning(
                "ride_story.invalid", activity_id=activity_id, attempt=attempt + 1,
                errors=problems[:5],
            )  # fmt: skip
            continue
        lines, poem, source = got.lines, got.poem, "llm"
        log.info(
            "ride_story.narrated",
            activity_id=activity_id,
            model=got.model,
            attempt=attempt + 1,
            input_tokens=got.input_tokens,
            output_tokens=got.output_tokens,
        )
        break
    story = Story(activity_id, kind, head, lines, poem, source, facts=facts.to_dict())
    return story, ok


def story(ctx: AppContext, activity_id: int) -> Story:
    """The ride's story for display: the cached one, else a deterministic one (cached).

    Never calls a network service; a route looked up earlier is used.

    Raises:
        NotFoundError: unknown activity.
    """
    cached = load_cached(ctx, activity_id)
    if cached is not None:
        return cached
    made, _ok = _compose(ctx, activity_id, _route_cached(ctx, activity_id), None)
    _write(_story_path(ctx, activity_id), made.to_dict())
    return made


def prepare(
    ctx: AppContext,
    activity_id: int,
    *,
    osm: OsmFactory | None = None,
    narrator: Narrator | None = None,
) -> Story:
    """Look the route up and narrate (each behind its flag), cache and return the story.

    A story prepared with the same flags is returned as is (a resend is identical). Never
    raises for an OSM or LLM failure: the story is then deterministic and retried next time.

    Raises:
        NotFoundError: unknown activity.
    """
    flags = _flags(ctx)
    cached = load_cached(ctx, activity_id)
    if cached is not None and cached.prepared and cached.flags == flags:
        return cached
    report = _report(ctx, activity_id)
    route: copy.RouteInfo | None = _route_cached(ctx, activity_id)
    ok = True
    if flags["route"] and route is None:
        client = (osm or (lambda: OsmClient(contact=ctx.settings.cyp_osm_contact)))()
        try:
            route = route_info(ctx, activity_id, report, client)
        except CypError as exc:
            log.warning("ride_story.osm_failed", activity_id=activity_id, error=str(exc))
            ok = False
        finally:
            client.close()
    if not flags["route"]:
        route = None
    narr: Narrator | None = None
    if flags["poem"]:
        key = ctx.settings.anthropic_api_key.get_secret_value()
        if narrator is not None:
            narr = narrator
        elif key:
            narr = ClaudeNarrator(api_key=key, model=ctx.settings.cyp_llm_model)
        else:
            log.warning("ride_story.no_api_key", activity_id=activity_id)
    made, narrated = _compose(ctx, activity_id, route, narr, report)
    done = Story(**{**made.__dict__, "prepared": ok and narrated, "flags": flags})
    _write(_story_path(ctx, activity_id), done.to_dict())
    return done
