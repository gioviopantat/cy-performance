"""Overpass client: named places in a bounding box (ADR-0021).

Takes only points that went through :mod:`cyp.ingest.osm.privacy`. At most one request per
:attr:`OsmClient.min_interval_s` per service (OSM usage policy), an identifying
User-Agent with an optional contact (``CYP_OSM_CONTACT``), no retries beyond one per call:
a failure raises :class:`OsmError` and the caller writes its text without route details.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from cyp import __version__
from cyp.core.errors import CypError
from cyp.ingest.osm.privacy import Point

#: Public Overpass instances, tried in order when one is overloaded (5xx, timeout).
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
#: A place counts as on the route within this distance of it (checked locally).
AROUND_M = 150
SERVER_TIMEOUT_S = 90
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
#: Kinds worth suggesting as a next destination, best first.
SPOT_KINDS = ("viewpoint", "peak", "attraction", "cafe")


class OsmError(CypError):
    """An Overpass request failed or returned something unusable."""


@dataclass(frozen=True)
class Place:
    """A named OSM feature near the route."""

    osm_id: str
    name: str
    kind: str
    lat: float
    lng: float
    #: Elevation in metres (OSM ``ele``), when tagged.
    ele: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict (the cache format)."""
        return {"osm_id": self.osm_id, "name": self.name, "kind": self.kind,
                "lat": self.lat, "lng": self.lng, "ele": self.ele}  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Place:
        """Inverse of :meth:`to_dict`."""
        ele = d.get("ele")
        return cls(str(d["osm_id"]), str(d["name"]), str(d["kind"]), float(d["lat"]),
                   float(d["lng"]), int(ele) if ele is not None else None)  # fmt: skip


def _name(tags: dict[str, Any]) -> str | None:
    for key in ("name:zh-Hant", "name:zh-TW", "name:zh", "name"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ele(tags: dict[str, Any]) -> int | None:
    """``ele`` as whole metres (``"512"``, ``"512.3"``, ``"512 m"``), else None."""
    try:
        return round(float(str(tags.get("ele", "")).replace("m", "").strip()))
    except ValueError:
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
        out[osm_id] = Place(osm_id, name, kind, float(lat), float(lng), _ele(tags))
    return list(out.values())


def _coords(points: Sequence[Point]) -> str:
    return ",".join(f"{la:.5f},{lo:.5f}" for la, lo in points)


def bbox_query(box: tuple[float, float, float, float], kinds: Sequence[str] = SPOT_KINDS) -> str:
    """Overpass QL: named places of ``kinds`` in ``(south, west, north, east)``."""
    b = ",".join(f"{v:.4f}" for v in box)
    parts = [f'nwr({b}){flt}["name"];' for k in kinds for flt in KINDS[k]]
    return f"[out:json][timeout:{SERVER_TIMEOUT_S}];({''.join(parts)});out center tags 300;"


@dataclass
class OsmClient:
    """Rate-limited Overpass client; ``transport`` and ``sleep`` are for tests."""

    contact: str = ""
    timeout_s: float = 60.0
    #: Wall-clock limit of one request (the watcher tick has 9 minutes for everything).
    total_timeout_s: float = 75.0
    #: Wall-clock budget of every request of this client together (one ride's lookup).
    budget_s: float = 180.0
    min_interval_s: float = 1.0
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _last: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _budget_end: float | None = field(default=None, init=False, repr=False)

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

    def _json(self, service: str, request: httpx.Request) -> dict[str, Any]:
        """Send ``request`` and parse its JSON, within :attr:`total_timeout_s` of wall clock.

        A busy Overpass keeps the connection alive while the query waits in its queue, so the
        per-read timeout alone could wait for many minutes; the body is read in chunks against
        a deadline instead.
        """
        if self._budget_end is None:
            self._budget_end = self.clock() + self.budget_s
        if self.clock() >= self._budget_end:
            raise OsmError(f"{service}: the {self.budget_s:.0f} s budget of this lookup is spent")
        self._wait(service)
        deadline = min(self.clock() + self.total_timeout_s, self._budget_end)
        try:
            response = self.http.send(request, stream=True)
            try:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if self.clock() > deadline:
                        raise OsmError(f"{service}: no answer in time")
            finally:
                response.close()
            data = json.loads(bytes(body))
        except (httpx.HTTPError, ValueError) as exc:
            raise OsmError(f"{service}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict):
            raise OsmError(f"{service}: unexpected answer")
        return data

    def _overpass(self, query: str) -> dict[str, Any]:
        """``query`` on the first Overpass instance that answers."""
        error: OsmError | None = None
        for url in OVERPASS_URLS:
            request = self.http.build_request("POST", url, data={"data": query})
            try:
                return self._json("overpass", request)
            except OsmError as exc:
                error = exc
        assert error is not None
        raise error

    def places_in(
        self, box: tuple[float, float, float, float], kinds: Sequence[str] = SPOT_KINDS
    ) -> list[Place]:
        """Named places of ``kinds`` in a (privacy-checked) bounding box.

        Only the box leaves; which places lie on the route is decided locally by the caller.
        """
        return parse_overpass(self._overpass(bbox_query(box, kinds)))
