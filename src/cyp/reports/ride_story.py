"""The casual Strava RIDE.LOG: facts, content type, copy and validation (spec casual-and-strava C).

The lines are concrete suggestions for next time, never a diary of the ride and never vague
filler (Roger, 2026-10-18): a line exists only when it has a real name or number behind it.

Pure: :func:`story_facts` turns the ride report (the PDF's facts) and the OSM route lookup into
a :class:`StoryFacts`; :func:`pick_type` and :func:`pick_head` choose what this post is about and
the acrostic head deterministically; :func:`deterministic_lines` writes the lines from
``i18n/ride_story.zh-TW.yaml``; :func:`validate` checks a narrator's output against the facts
(ADR-0021: the LLM may phrase, never compute or invent). No verdicts, plan or jargon here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from cyp.ingest.osm.client import Place
from cyp.reports.ride_model import ClimbCompare, RideReport
from cyp.settings import ClimbConfig

STORY_VERSION = "ride_story_v3"
COPY_PATH = Path(__file__).resolve().parent / "i18n" / "ride_story.zh-TW.yaml"
MARKER = "⚙️ RIDE.LOG"
#: Rotation order; a ride only gets types it has material for.
TYPES = ("next_spot", "climb", "stop", "weather")
#: A ride with nothing concrete to suggest gets no RIDE.LOG at all.
NONE = "none"
#: A casual ride (tag A) prefers a next spot, then its stop.
CASUAL_ORDER = ("next_spot", "stop")
RECENT_HEADS = 10
MAX_LINES, MIN_LINES, MAX_LINE_CHARS = 2, 1, 60
POEM_LINES, POEM_MIN_CHARS, POEM_MAX_CHARS = 4, 5, 9
#: Place-like and forbidden words live in the copy file (``validation``).
#: The only units a public post may use (no W, kJ, bpm, kg: they reveal FTP, weight, health).
UNITS = re.compile(r"(?<![A-Za-z])(km|m)(?![A-Za-z])|°C")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
CJK = re.compile(f"[{chr(0x3400)}-{chr(0x9FFF)}]")
QUOTED = re.compile(r"「([^」]+)」")


@cache
def _copy() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COPY_PATH.read_text(encoding="utf-8"))
    return data


def _pick(options: Sequence[str], seed: int) -> str:
    return options[seed % len(options)]


@dataclass(frozen=True)
class RouteInfo:
    """What the OSM lookup found for one ride (cached per ride by the service)."""

    places: list[Place] = field(default_factory=list)
    stop: Place | None = None
    spot: Place | None = None
    spot_km: int | None = None
    #: Compass direction from the turnaround to the spot (``N`` ... ``NW``).
    spot_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return {
            "spot_dir": self.spot_dir,
            "places": [p.to_dict() for p in self.places],
            "stop": self.stop.to_dict() if self.stop else None,
            "spot": self.spot.to_dict() if self.spot else None,
            "spot_km": self.spot_km,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> RouteInfo:
        """Inverse of :meth:`to_dict`."""
        return cls(
            places=[Place.from_dict(p) for p in d.get("places") or []],
            stop=Place.from_dict(d["stop"]) if d.get("stop") else None,
            spot=Place.from_dict(d["spot"]) if d.get("spot") else None,
            spot_km=d.get("spot_km"),
            spot_dir=d.get("spot_dir"),
        )


@dataclass(frozen=True)
class StoryFacts:
    """Everything the RIDE.LOG may say; the narrator gets exactly this (as JSON)."""

    activity_id: int
    month: int
    casual: bool
    indoor: bool
    km: float | None
    elev_m: int | None
    minutes: int
    temp_c: int | None
    #: A named climb of the athlete's config this ride went up, its rank among their ascents
    #: and the gap to their fastest (seconds; None when this one is the fastest).
    climb: str | None = None
    climb_rank: int | None = None
    climb_of_n: int | None = None
    climb_gap_s: int | None = None
    stop: str | None = None
    spot: str | None = None
    spot_kind: str | None = None
    spot_km: int | None = None
    spot_dir: str | None = None
    spot_ele: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict."""
        return asdict(self)

    def place_names(self) -> set[str]:
        """Every place name the text may use."""
        return {n for n in (self.climb, self.stop, self.spot) if n}

    def numbers(self) -> set[str]:
        """Every number the text may quote, as written."""
        out: set[str] = {str(self.month), str(self.minutes)}
        for v in (self.elev_m, self.temp_c, self.climb_rank, self.climb_of_n, self.spot_km,
                  self.spot_ele, self.climb_gap_s):  # fmt: skip
            if v is not None:
                out.add(str(v))
        if self.climb_gap_s is not None:
            out |= {str(self.climb_gap_s // 60), str(self.climb_gap_s % 60)}
        if self.km is not None:
            out |= {f"{self.km:.1f}", str(round(self.km))}
        return out


#: How close a ride's climb must be to a configured one to carry its name.
CLIMB_DISTANCE_TOL, CLIMB_GRADE_TOL_PP = 0.12, 1.0


def named_climb(c: ClimbCompare, named: Sequence[ClimbConfig]) -> str | None:
    """The athlete's name for a detected climb: the closest configured one within tolerance."""
    if c.distance_km is None or c.grade_pct is None:
        return None
    fits = [
        (abs(n.distance_km - c.distance_km) / n.distance_km + abs(n.grade_pct - c.grade_pct) / 10,
         n.name_zh)
        for n in named
        if abs(n.distance_km - c.distance_km) <= CLIMB_DISTANCE_TOL * n.distance_km
        and abs(n.grade_pct - c.grade_pct) <= CLIMB_GRADE_TOL_PP
    ]  # fmt: skip
    return min(fits)[1] if fits else None


def compass(a: tuple[float, float], b: tuple[float, float]) -> str:
    """8-point compass direction from ``a`` to ``b`` (lat, lng)."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlng = math.radians(b[1] - a[1])
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(bearing / 45) % 8]


def story_facts(
    report: RideReport,
    route: RouteInfo | None,
    *,
    casual: bool,
    named: Sequence[ClimbConfig] = (),
) -> StoryFacts:
    """The facts of one ride from its report (the PDF's numbers) and the OSM lookup.

    ``named``: the athlete's configured climbs; a detected climb only counts with their name.
    """
    r = report.ride
    ranked = [
        (c, name)
        for c in report.climbs
        if c.rank is not None and c.of_n is not None and c.of_n > 1
        and (name := named_climb(c, named)) is not None
    ]  # fmt: skip
    best = min(ranked, key=lambda x: x[0].rank or 0, default=None)
    gap: int | None = None
    if best is not None and best[0].rank != 1:
        fastest = min((e.time_s for e in best[0].history), default=None)
        gap = best[0].this.time_s - fastest if fastest is not None else None
    temp = report.outdoor.temp_avg_c if report.outdoor is not None else None
    route = route or RouteInfo()
    spot = route.spot
    return StoryFacts(
        activity_id=r.activity_id,
        month=r.start_local.month,
        casual=casual,
        indoor=r.indoor,
        km=round(r.distance_km, 1) if r.distance_km is not None else None,
        elev_m=r.elev_gain_m,
        minutes=r.moving_min,
        temp_c=round(temp) if temp is not None and not r.indoor else None,
        climb=best[1] if best is not None else None,
        climb_rank=best[0].rank if best is not None else None,
        climb_of_n=best[0].of_n if best is not None else None,
        climb_gap_s=gap if gap is not None and gap > 0 else None,
        stop=route.stop.name if route.stop is not None else None,
        spot=spot.name if spot is not None else None,
        spot_kind=spot.kind if spot is not None else None,
        spot_km=route.spot_km,
        spot_dir=route.spot_dir,
        spot_ele=spot.ele if spot is not None and spot.kind == "peak" else None,
    )


#: Below / above these an outdoor ride's temperature is worth a tip.
COLD_C, HOT_C = 12, 30


def available_types(f: StoryFacts) -> list[str]:
    """The content types this ride has something concrete for (may be none)."""
    has = {
        "next_spot": f.spot is not None and f.spot_km is not None,
        "climb": f.climb is not None,
        "stop": f.stop is not None,
        "weather": f.temp_c is not None and not COLD_C <= f.temp_c < HOT_C,
    }
    return [t for t in TYPES if has[t]]


def pick_type(f: StoryFacts, previous: str | None) -> str:
    """This post's content type: a rotation by activity id that never repeats the previous one.

    A casual ride takes a next spot or its stop first when it has one.
    """
    available = available_types(f)
    if not available:
        return NONE
    if f.casual:
        for t in CASUAL_ORDER:
            if t in available and t != previous:
                return t
    choices = [t for t in available if t != previous] or available
    return _pick(choices, f.activity_id)


def pick_head(kind: str, activity_id: int, recent: Iterable[str] = ()) -> str:
    """A 4-character acrostic head for ``kind``, not one of the recently used heads."""
    if kind == NONE:
        return ""
    heads: list[str] = list(_copy()["heads"][kind])
    seen = set(recent)
    fresh = [h for h in heads if h not in seen] or heads
    return _pick(fresh, activity_id)


def _gap(seconds: int) -> str:
    m, sec = divmod(seconds, 60)
    return str(_copy()["gap"]["min_sec" if m else "sec"]).format(m=m, s=sec)


def _main_line(f: StoryFacts, kind: str) -> str:
    c, seed = _copy(), f.activity_id
    if kind == "climb":
        if f.climb_rank == 1:
            return _pick(c["climb_best"], seed).format(climb=f.climb, of_n=f.climb_of_n)
        if f.climb_gap_s is None:
            return _pick(c["climb_rank"], seed).format(
                climb=f.climb, of_n=f.climb_of_n, rank=f.climb_rank
            )
        return _pick(c["climb_gap"], seed).format(
            climb=f.climb, of_n=f.climb_of_n, rank=f.climb_rank, gap=_gap(f.climb_gap_s)
        )
    if kind == "next_spot":
        why = c["spot_why"].get(f.spot_kind or "", c["spot_why"]["attraction"])
        if f.spot_ele is not None:
            why = str(c["spot_why"]["peak_ele"]).format(ele=f.spot_ele)
        args = {"spot": f.spot, "why": why, "km": f.spot_km}
        if f.spot_dir is not None:
            return _pick(c["next_spot_dir"], seed).format(dir=c["dir"][f.spot_dir], **args)
        return _pick(c["next_spot"], seed).format(**args)
    if kind == "stop":
        return _pick(c["stop"], seed).format(stop=f.stop)
    assert f.temp_c is not None
    key = "hot" if f.temp_c >= HOT_C else "cold"
    return _pick(c["weather"][key], seed).format(temp=f.temp_c)


def deterministic_lines(f: StoryFacts, kind: str) -> list[str]:
    """0–2 concrete suggestions: the content type's, plus a next spot when the ride has one."""
    if kind == NONE:
        return []
    lines = [_main_line(f, kind)]
    if kind != "next_spot" and "next_spot" in available_types(f):
        lines.append(_main_line(f, "next_spot"))
    return lines


def compose_block(lines: Sequence[str], poem: Sequence[str] | None = None) -> str:
    """The RIDE.LOG block: marker, lines, quoted poem, hashtag; one paragraph (no blank line).

    No lines: no block (``""``).
    """
    if not lines:
        return ""
    out = [MARKER, *lines]
    out += [f"> {p}" for p in poem or ()]
    out.append(str(_copy()["footer"]))
    return "\n".join(out)


def _latin_free(text: str) -> bool:
    return not re.search(r"[A-Za-z]", UNITS.sub("", text))


def validate(
    lines: Sequence[str],
    poem: Sequence[str],
    f: StoryFacts,
    head: str,
    draft: Sequence[str] | None = None,
) -> list[str]:
    """Problems of a narrator's output (empty = valid); see the spec for each rule.

    With ``draft`` (the deterministic lines the narrator rephrases) the output may only name
    the places and quote the numbers the draft does: a rephrasing adds no claim.
    """
    errors: list[str] = []
    if not MIN_LINES <= len(lines) <= MAX_LINES:
        errors.append(f"{len(lines)} lines")
    if len(poem) != POEM_LINES:
        errors.append(f"{len(poem)} poem lines")
    names, numbers = f.place_names(), f.numbers()
    if draft is not None:
        joined = "".join(draft)
        names = {n for n in names if n in joined}
        numbers = set(NUMBER.findall(joined))
        for other in sorted(f.place_names() - names, key=len, reverse=True):
            if any(other in text for text in [*lines, *poem]):
                errors.append(f"place {other} is not in the draft")
    for text in [*lines, *poem]:
        if not text.strip():
            errors.append("empty line")
        if not _latin_free(text):
            errors.append(f"latin letters: {text}")
        for word in _copy()["validation"]["forbidden"]:
            if word in text:
                errors.append(f"forbidden word {word}: {text}")
        for n in NUMBER.findall(text):
            if n not in numbers:
                errors.append(f"number {n} not in the {'draft' if draft is not None else 'facts'}")
        for quoted in QUOTED.findall(text):
            if quoted not in names:
                errors.append(f"unknown place 「{quoted}」")
    for text in lines:
        if len(text) > MAX_LINE_CHARS:
            errors.append(f"line too long: {text}")
        rest = text
        for name in sorted(names, key=len, reverse=True):
            rest = rest.replace(name, "")
        for word in _copy()["validation"]["place_words"]:
            if word in rest:
                errors.append(f"place-like word {word} outside the facts: {text}")
    for i, text in enumerate(poem):
        chars = CJK.findall(text)
        if not POEM_MIN_CHARS <= len(chars) <= POEM_MAX_CHARS:
            errors.append(f"poem line {i + 1} has {len(chars)} characters")
        if i < len(head) and (not chars or chars[0] != head[i]):
            errors.append(f"poem line {i + 1} does not start with {head[i]}")
    return errors


def head_chars(poem: Sequence[str]) -> str:
    """The acrostic a poem spells (first Chinese character of each line)."""
    return "".join(next(iter(CJK.findall(p)), "") for p in poem)
