"""Casual RIDE.LOG: privacy zone, OSM cache, variety, validation, narrator (spec C).

Strings: ``tests/fixtures/ride_story.zh-TW.yaml``. No network: OpenStreetMap is an
``httpx.MockTransport`` that records every request, Claude a fake client or a fake narrator.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

from cyp.ingest.osm import privacy
from cyp.ingest.osm.client import OsmClient, Place
from cyp.llm.narrator import ClaudeNarrator, Narration, NarrationError
from cyp.reports import ride_story as copy
from cyp.services import ride_story
from cyp.services.context import AppContext
from cyp.store.models import Activity

T: dict[str, Any] = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ride_story.zh-TW.yaml").read_text(
        encoding="utf-8"
    )
)

ROUTE = "strava.route_story=on"
POEM = "llm.ride_poem=on"
COORD = re.compile(r"(-?\d+\.\d+),(-?\d+\.\d+)")


def _with(ctx: AppContext, features: str, **settings: Any) -> None:
    ctx.settings = ctx.settings.model_copy(update={"cyp_features": features, **settings})


def _rides(ctx: AppContext) -> list[int]:
    with ctx.factory() as s:
        return [
            a.id
            for a in s.query(Activity)
            .filter(Activity.is_ride.is_(True))
            .order_by(Activity.start_local)
            .all()
        ]


class FakeOsm:
    """Overpass + Nominatim stand-in; ``requests`` keeps what would have been sent."""

    def __init__(self, ctx: AppContext, aid: int, *, fail: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.fail = fail
        pts = ride_story._points(ctx, aid)
        mid = pts[len(pts) // 2]
        self.start, self.end = pts[0], pts[-1]
        home = (self.start[0] + 0.001, self.start[1])  # within 1 km of the start
        self.along = [
            {"type": "node", "id": 1, "lat": mid[0], "lon": mid[1],
             "tags": {"amenity": "cafe", "name": T["places"]["cafe"]}},
            {"type": "node", "id": 2, "lat": home[0], "lon": home[1],
             "tags": {"amenity": "cafe", "name": T["places"]["home_cafe"]}},
            {"type": "way", "id": 3, "center": {"lat": mid[0] + 0.002, "lon": mid[1]},
             "tags": {"natural": "peak", "name": T["places"]["peak"]}},
        ]  # fmt: skip
        far = (mid[0] + 0.1, mid[1])
        self.box = [
            {"type": "node", "id": 9, "lat": far[0], "lon": far[1],
             "tags": {"tourism": "viewpoint", "name": T["places"]["viewpoint"]}},
            {"type": "node", "id": 1, "lat": mid[0], "lon": mid[1],
             "tags": {"amenity": "cafe", "name": T["places"]["cafe"]}},
        ]  # fmt: skip

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(503)
        if "nominatim" in request.url.host:
            return httpx.Response(
                200,
                json={"address": {"suburb": T["places"]["district"], "city": T["places"]["city"]}},
            )
        query = parse_qs(request.content.decode())["data"][0]
        return httpx.Response(
            200, json={"elements": self.box if "around" not in query else self.along}
        )

    def client(self) -> OsmClient:
        return OsmClient(
            contact="test@example.invalid",
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _s: None,
        )

    def sent_points(self) -> list[privacy.Point]:
        out: list[privacy.Point] = []
        for r in self.requests:
            if "nominatim" in r.url.host:
                q = parse_qs(r.url.query.decode())
                out.append((float(q["lat"][0]), float(q["lon"][0])))
                continue
            query = parse_qs(r.content.decode())["data"][0]
            if "around" in query:
                out += [(float(a), float(b)) for a, b in COORD.findall(query)]
        return out


# ------------------------------------------------------------------------------ privacy


def test_trimmed_route_drops_the_privacy_zone_and_samples() -> None:
    start = (25.0, 121.4)
    line = [(start[0] + i * 0.0005, start[1]) for i in range(200)]  # ~11 km north
    out = privacy.trimmed_route(line)
    assert out and all(privacy.outside_zone(p, line[0], line[-1]) for p in out)
    gaps = [privacy.haversine_m(a, b) for a, b in itertools.pairwise(out)]
    assert min(gaps) >= privacy.STEP_M - 1
    assert privacy.trimmed_route(line[:10]) == []  # a short loop near home sends nothing


def test_no_coordinate_near_start_or_end_ever_leaves(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, ROUTE)
    fake = FakeOsm(ctx, aid)
    story = ride_story.prepare(ctx, aid, osm=fake.client)
    sent = fake.sent_points()
    assert sent, "the route was looked up"
    assert all(privacy.outside_zone(p, fake.start, fake.end) for p in sent)
    for r in fake.requests:
        body = (r.content or b"").decode() + str(r.url)
        assert "t_s" not in body and "activity" not in body and "athlete" not in body
        assert r.headers["User-Agent"].startswith("cy-performance/")
        assert "test@example.invalid" in r.headers["User-Agent"]
    assert sum("nominatim" in r.url.host for r in fake.requests) <= 3
    # A place next to home is never named; the route's café and peak are.
    route = copy.RouteInfo.from_dict(
        json.loads((ride_story.story_dir(ctx) / "osm" / f"{aid}.json").read_text())["route"]
    )
    names = [p.name for p in route.places]
    assert T["places"]["home_cafe"] not in names and {
        T["places"]["cafe"],
        T["places"]["peak"],
    } <= set(names)
    assert (
        route.spot is not None and route.spot.name == T["places"]["viewpoint"]
    )  # visited café skipped
    assert route.districts == [T["places"]["district"]] and story.prepared


def test_osm_cache_and_visited_record(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, ROUTE)
    fake = FakeOsm(ctx, aid)
    first = ride_story.prepare(ctx, aid, osm=fake.client)
    n = len(fake.requests)
    again = ride_story.prepare(ctx, aid, osm=fake.client)
    assert len(fake.requests) == n and again == first  # cached: identical, no request
    visited = json.loads((ride_story.story_dir(ctx) / "visited.json").read_text())
    assert visited.get("node/1") == T["places"]["cafe"]


def test_osm_failure_gives_the_deterministic_text_and_retries(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, ROUTE)
    broken = FakeOsm(ctx, aid, fail=True)
    s = ride_story.prepare(ctx, aid, osm=broken.client)
    assert s.source == "deterministic" and not s.prepared
    assert not (ride_story.story_dir(ctx) / "osm" / f"{aid}.json").exists()
    fixed = FakeOsm(ctx, aid)
    assert ride_story.prepare(ctx, aid, osm=fixed.client).source == "route"


def test_flags_off_never_call_out(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    fake = FakeOsm(ctx, aid)
    calls: list[Any] = []
    s = ride_story.prepare(ctx, aid, osm=fake.client, narrator=lambda f, h: calls.append(f))
    assert fake.requests == [] and calls == [] and s.poem == []
    assert ride_story.story(ctx, aid) == s


# ------------------------------------------------------------------------------ variety


def _facts(aid: int, **kw: Any) -> copy.StoryFacts:
    base: dict[str, Any] = {"activity_id": aid, "month": 10, "casual": False, "indoor": False,
                            "km": 42.3, "elev_m": 380, "minutes": 95, "temp_c": 27}  # fmt: skip
    return copy.StoryFacts(**{**base, **kw})


def test_no_two_consecutive_posts_share_a_content_type() -> None:
    previous: str | None = None
    kinds = []
    for aid in range(1, 60):
        f = _facts(
            aid,
            districts=[T["places"]["linkou"]] if aid % 2 else [],
            stop=T["places"]["cafe"] if aid % 3 else None,
        )
        kind = copy.pick_type(f, previous)
        assert kind != previous
        kinds.append(kind)
        previous = kind
    assert len(set(kinds)) >= 4  # varied, not two alternating types


def test_service_variety_across_rides(ctx: AppContext) -> None:
    kinds = [ride_story.story(ctx, aid).kind for aid in _rides(ctx)[-6:]]
    assert all(a != b for a, b in itertools.pairwise(kinds))


def test_casual_ride_leans_on_the_stop_and_company() -> None:
    f = _facts(7, casual=True, stop=T["places"]["cafe"], districts=[T["places"]["linkou"]])
    assert copy.pick_type(f, None) == "stop"
    assert copy.pick_type(f, "stop") == "route"
    lines = copy.deterministic_lines(f, "stop")
    assert f"「{T['places']['cafe']}」" in lines[0] and T["company"] in lines[-1]


def test_heads_skip_recent_ones() -> None:
    heads = [copy.pick_head("route", 3, [])]
    for _ in range(5):
        heads.append(copy.pick_head("route", 3, heads))
    assert len(set(heads)) == len(heads) and all(len(h) == 4 for h in heads)


def test_deterministic_lines_are_casual_and_valid() -> None:
    P = T["places"]
    f = _facts(
        5,
        districts=[P["linkou"], P["bali"]],
        stop=P["cafe"],
        spot=P["viewpoint"],
        spot_kind="viewpoint",
        spot_km=12,
        climb=P["climb"],
        climb_rank=2,
        climb_of_n=9,
    )
    for kind in copy.available_types(f):
        lines = copy.deterministic_lines(f, kind)
        assert copy.validate(lines, [T["filler"] * 7] * 4, f, T["filler"] * 4) == [], (kind, lines)
        assert not any(w in "".join(lines) for w in (*T["banned_text"], "STATUS", "TSB"))


# ----------------------------------------------------------------------------- validation


POEM_OK: list[str] = T["poem_ok"]


def test_validate_accepts_a_good_answer() -> None:
    f = _facts(1, districts=[T["places"]["linkou"]], stop=T["places"]["cafe"])
    lines = T["lines"]["good"]
    assert copy.validate(lines, POEM_OK, f, T["poem_head"]) == []
    assert copy.head_chars(POEM_OK) == T["poem_head"]


@pytest.mark.parametrize(
    ("lines", "poem", "problem"),
    [
        (T["lines"]["invented"], POEM_OK, "unknown place"),
        (T["lines"]["place_word"], POEM_OK, "place-like word"),
        (T["lines"]["number"], POEM_OK, "number 99"),
        (T["lines"]["latin"], POEM_OK, "latin"),
        (T["lines"]["forbidden"], POEM_OK, "forbidden"),
        (T["lines"]["one"], POEM_OK, "1 lines"),
        (T["lines"]["plain"], POEM_OK[:3], "3 poem lines"),
        (T["lines"]["plain"], [T["poem_bad_head"], *POEM_OK[1:]], "does not start with"),
        (T["lines"]["plain"], [T["poem_short"], *POEM_OK[1:]], "has 1 characters"),
    ],
)
def test_validate_rejects(lines: list[str], poem: list[str], problem: str) -> None:
    f = _facts(1, districts=[T["places"]["linkou"]], stop=T["places"]["cafe"])
    errors = copy.validate(lines, poem, f, T["poem_head"])
    assert any(problem in e for e in errors), errors


# ------------------------------------------------------------------------------ narrator


def test_valid_narration_is_used_and_cached(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, POEM)
    seen: list[dict[str, Any]] = []

    def narrator(facts: dict[str, Any], head: str) -> Narration:
        seen.append(facts)
        km = f"{facts['km']:.1f}"
        poem = [f"{c}{T['mountain'] * 6}" for c in head]
        lines = [x.format(km=km) for x in T["lines"]["narrated"]]
        return Narration(lines, poem, "claude-opus-5-5")

    s = ride_story.prepare(ctx, aid, narrator=narrator)
    assert s.source == "llm" and len(s.poem) == 4 and s.prepared
    assert copy.head_chars(s.poem) == s.head
    assert set(seen[0]) >= {"content_type", "km", "districts"} and "athlete" not in json.dumps(seen)
    text = ride_story.story(ctx, aid).text
    assert text.count("\n> ") == 4 and text.endswith("#cyperformance")
    assert ride_story.prepare(ctx, aid, narrator=lambda f, h: None) == s  # cached


def test_invented_place_falls_back_to_the_deterministic_text(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, POEM)

    def liar(facts: dict[str, Any], head: str) -> Narration:
        return Narration(T["lines"]["liar"], [c * 7 for c in head], "m")

    s = ride_story.prepare(ctx, aid, narrator=liar)
    assert s.source != "llm" and s.poem == [] and T["places"]["invented2"][:2] not in s.text


def test_refusal_and_api_error(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, POEM)
    assert ride_story.prepare(ctx, aid, narrator=lambda f, h: None).poem == []
    other = _rides(ctx)[-2]

    def down(facts: dict[str, Any], head: str) -> Narration:
        raise NarrationError("APIConnectionError: offline")

    s = ride_story.prepare(ctx, other, narrator=down)
    assert s.poem == [] and not s.prepared  # retried on the next prepare


def test_no_key_means_no_call(ctx: AppContext) -> None:
    aid = _rides(ctx)[-1]
    _with(ctx, POEM)  # ANTHROPIC_API_KEY empty in tests
    assert ride_story.prepare(ctx, aid).poem == []


class _FakeMessages:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _client(response: Any) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(response)))


def test_claude_narrator_request_and_refusal() -> None:
    ok = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-opus-5-5",
        content=[
            SimpleNamespace(
                type="text",
                text=json.dumps({"lines": ["a", "b"], "poem": T["claude_poem"]}),
            )
        ],
        usage=SimpleNamespace(input_tokens=900, output_tokens=200),
    )
    client = _client(ok)
    got = ClaudeNarrator(api_key="test", client=client)({"km": 1.0}, T["claude_head"])
    assert (
        got is not None
        and got.poem == [x.lstrip("> ") for x in T["claude_poem"]]
        and got.output_tokens == 200
    )
    kw = client.beta.messages.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 1024
    assert kw["output_config"]["effort"] == "low" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert T["rule_word"] in kw["system"] and "km" in kw["messages"][0]["content"]
    refused = SimpleNamespace(stop_reason="refusal", content=[], model="m", usage=None)
    assert ClaudeNarrator(api_key="test", client=_client(refused))({}, T["claude_head"]) is None
    garbage = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="no")],
        model="m",
        usage=None,
    )
    assert ClaudeNarrator(api_key="test", client=_client(garbage))({}, T["claude_head"]) is None


def test_place_round_trip() -> None:
    p = Place("node/1", T["places"]["cafe"], "cafe", 25.1, 121.4)
    assert Place.from_dict(p.to_dict()) == p


def test_cli_show_fetch_runs_prepare(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from cyp.cli import app

    called: list[int] = []
    monkeypatch.setattr(ride_story, "prepare", lambda c, aid, **_k: called.append(aid))
    env = {"CYP_DATA_DIR": str(ctx.settings.cyp_data_dir), "CYP_DB_URL": ctx.settings.cyp_db_url}
    aid = _rides(ctx)[-1]
    plain = CliRunner().invoke(app, ["ride-log", "show", str(aid)], env=env)
    assert plain.exit_code == 0, plain.output and called == []
    assert "⚙️ RIDE.LOG" in plain.output
    fetched = CliRunner().invoke(app, ["ride-log", "show", str(aid), "--fetch"], env=env)
    assert fetched.exit_code == 0 and called == [aid]
