"""Privacy zone and sampling for coordinates sent to OpenStreetMap services (ADR-0021).

Every point within :data:`PRIVACY_RADIUS_M` of the ride's start or end is dropped (home is
usually one of them); the rest is sampled every :data:`STEP_M` metres and rounded to
:data:`DECIMALS` decimals. Only these points (no timestamps, no ids) may be sent out.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

PRIVACY_RADIUS_M = 1000.0
STEP_M = 200.0
DECIMALS = 5
EARTH_RADIUS_M = 6_371_000.0

Point = tuple[float, float]


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres between two ``(lat, lng)`` points."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def valid_points(lat: Iterable[float], lng: Iterable[float]) -> list[Point]:
    """``(lat, lng)`` pairs without gaps (NaN) or the 0/0 a GPS writes before a fix."""
    out: list[Point] = []
    for la, lo in zip(lat, lng, strict=False):
        if math.isnan(la) or math.isnan(lo) or (la == 0.0 and lo == 0.0):
            continue
        if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
            continue
        out.append((float(la), float(lo)))
    return out


def endpoints(points: Sequence[Point]) -> tuple[Point, Point] | None:
    """The ride's start and end (``None`` without points)."""
    return (points[0], points[-1]) if points else None


def outside_zone(p: Point, start: Point, end: Point, radius_m: float = PRIVACY_RADIUS_M) -> bool:
    """``p`` is farther than ``radius_m`` from both the start and the end."""
    return haversine_m(p, start) >= radius_m and haversine_m(p, end) >= radius_m


def rounded(p: Point) -> Point:
    """The point as it is sent out."""
    return (round(p[0], DECIMALS), round(p[1], DECIMALS))


def trimmed_route(
    points: Sequence[Point], radius_m: float = PRIVACY_RADIUS_M, step_m: float = STEP_M
) -> list[Point]:
    """The route outside the privacy zone, one rounded point per ``step_m`` metres."""
    ends = endpoints(points)
    if ends is None:
        return []
    start, end = ends
    out: list[Point] = []
    last: Point | None = None
    for p in points:
        if not outside_zone(p, start, end, radius_m):
            last = None  # a new stretch after the zone starts with its first point
            continue
        if last is None or haversine_m(last, p) >= step_m:
            out.append(rounded(p))
            last = p
    return out


def bbox(points: Iterable[Point]) -> tuple[float, float, float, float] | None:
    """``(south, west, north, east)`` of ``points`` (``None`` without points)."""
    pts = list(points)
    if not pts:
        return None
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    return (min(lats), min(lngs), max(lats), max(lngs))
