"""Overpass (places near the route) and Nominatim (district names) client (ADR-0021).

Takes only points that went through :mod:`cyp.ingest.osm.privacy`. At most one request per
:attr:`OsmClient.min_interval_s` per service (Nominatim's usage policy), an identifying
User-Agent with an optional contact (``CYP_OSM_CONTACT``), no retries beyond one per call:
a failure raises :class:`OsmError` and the caller writes its text without route details.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from cyp import __version__
from cyp.core.errors import CypError
from cyp.ingest.osm.privacy import Point

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
AROUND_M = 150
#: ``kind`` of a place -> the Overpass filters that find it (all with a name).
KINDS: dict[str, tuple[str, ...]] = {
    "cafe": ('["amenity"="cafe"]', '["shop"="coffee"]'),
    "drink": ('["shop"~"^(beverages|tea|bubble_tea)$"]',),
    "viewpoint": ('["tourism"="viewpoint"]',),
    "attraction": ('["tourism"="attraction"]',),
    "peak": ('["natural"="peak"]',),
    "park": ('["leisure"="park"]',),
    "temple": ('["amenity"="place_of_worship"]',),
}
#: Kinds worth suggesting as a next destination.
SPOT_KINDS = ("viewpoint", "peak", "cafe", "attraction")
#: Address keys of a Nominatim result, most local first, that name a district.
DISTRICT_KEYS = ("suburb", "city_district", "town", "village", "county", "city")


class OsmError(CypError):
    """An Overpass or Nominatim request failed or returned something unusable."""


@dataclass(frozen=True)
class Place:
    """A named OSM feature near the route."""

    osm_id: str
    name: str
    kind: str
    lat: float
    lng: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (the cache format)."""
        return {"osm_id": self.osm_id, "name": self.name, "kind": self.kind,
                "lat": self.lat, "lng": self.lng}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Place:
        """Inverse of :meth:`to_dict`."""
        return cls(str(d["osm_id"]), str(d["name"]), str(d["kind"]), float(d["lat"]),
                   float(d["lng"]))  # fmt: skip


def _name(tags: dict[str, Any]) -> str | None:
    for key in ("name:zh-Hant", "name:zh-TW", "name:zh", "name"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _kind(tags: dict[str, Any]) -> str | None:
    amenity, shop, tourism = tags.get("amenity"), tags.get("shop"), tags.get("tourism")
    if amenity == "cafe" or shop == "coffee":
        return "cafe"
    if shop in ("beverages", "tea", "bubble_tea"):
        return "drink"
    if tourism in ("viewpoint", "attraction"):
        return str(tourism)
    if tags.get("natural") == "peak":
        return "peak"
    if tags.get("leisure") == "park":
        return "park"
    if amenity == "place_of_worship":
        return "temple"
    return None


def parse_overpass(data: dict[str, Any]) -> list[Place]:
    """Named places of an Overpass ``out center`` answer (one per OSM element)."""
    out: dict[str, Place] = {}
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name, kind = _name(tags), _kind(tags)
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lng = el.get("lon", (el.get("center") or {}).get("lon"))
        if name is None or kind is None or lat is None or lng is None:
            continue
        osm_id = f"{el.get('type', 'node')}/{el.get('id')}"
        out[osm_id] = Place(osm_id, name, kind, float(lat), float(lng))
    return list(out.values())


def _coords(points: Sequence[Point]) -> str:
    return ",".join(f"{la:.5f},{lo:.5f}" for la, lo in points)


def along_query(points: Sequence[Point], kinds: Sequence[str] = tuple(KINDS)) -> str:
    """Overpass QL: named places of ``kinds`` within :data:`AROUND_M` of the route line."""
    around = f"(around:{AROUND_M},{_coords(points)})"
    parts = [f'nwr{around}{flt}["name"];' for k in kinds for flt in KINDS[k]]
    return f"[out:json][timeout:25];({''.join(parts)});out center tags;"


def bbox_query(box: tuple[float, float, float, float], kinds: Sequence[str] = SPOT_KINDS) -> str:
    """Overpass QL: named places of ``kinds`` in ``(south, west, north, east)``."""
    b = ",".join(f"{v:.4f}" for v in box)
    parts = [f'nwr({b}){flt}["name"];' for k in kinds for flt in KINDS[k]]
    return f"[out:json][timeout:25];({''.join(parts)});out center tags 300;"


def district_of(data: dict[str, Any]) -> str | None:
    """The most local district name of a Nominatim reverse result."""
    address = data.get("address") or {}
    for key in DISTRICT_KEYS:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass
class OsmClient:
    """Rate-limited Overpass + Nominatim client; ``transport`` and ``sleep`` are for tests."""

    contact: str = ""
    timeout_s: float = 30.0
    min_interval_s: float = 1.0
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _last: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    @property
    def user_agent(self) -> str:
        """``cy-performance/<version>`` plus the optional contact (OSM usage policy)."""
        contact = f" (+{self.contact.strip()})" if self.contact.strip() else ""
        return f"cy-performance/{__version__}{contact}"

    @property
    def http(self) -> httpx.Client:
        """Lazily created client with the User-Agent."""
        if self._http is None:
            self._http = httpx.Client(
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                timeout=self.timeout_s,
                transport=self.transport,
            )
        return self._http

    def close(self) -> None:
        """Close the connection pool."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def _wait(self, service: str) -> None:
        last = self._last.get(service)
        if last is not None:
            gap = self.min_interval_s - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[service] = self.clock()

    def _json(self, service: str, request: Callable[[], httpx.Response]) -> dict[str, Any]:
        self._wait(service)
        try:
            response = request()
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OsmError(f"{service}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict):
            raise OsmError(f"{service}: unexpected answer")
        return data

    def places_along(self, points: Sequence[Point]) -> list[Place]:
        """Named places near the (privacy-trimmed) route."""
        if not points:
            return []
        query = along_query(points)
        data = self._json("overpass", lambda: self.http.post(OVERPASS_URL, data={"data": query}))
        return parse_overpass(data)

    def places_in(self, box: tuple[float, float, float, float]) -> list[Place]:
        """Named viewpoints, peaks, cafés and attractions in a bounding box."""
        query = bbox_query(box)
        data = self._json("overpass", lambda: self.http.post(OVERPASS_URL, data={"data": query}))
        return parse_overpass(data)

    def district(self, p: Point) -> str | None:
        """The district a (privacy-checked) point is in."""
        params = {"format": "jsonv2", "lat": f"{p[0]:.5f}", "lon": f"{p[1]:.5f}", "zoom": "14",
                  "accept-language": "zh-TW", "addressdetails": "1"}  # fmt: skip
        return district_of(
            self._json("nominatim", lambda: self.http.get(NOMINATIM_URL, params=params))
        )
