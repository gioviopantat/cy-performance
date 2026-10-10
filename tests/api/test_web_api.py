"""Web UI endpoints: profile routing, autopilot gate, ride-log, OpenAPI drift (spec web-ui)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cyp.api.app import create_app
from cyp.api.registry import PROFILE_HEADER, ProfileRegistry
from cyp.core.errors import ConfigError
from cyp.profiles import set_planner_mode
from cyp.services.context import AppContext
from tests.api.conftest import make_ctx

REPO = Path(__file__).resolve().parents[2]


def test_openapi_json_is_current(ctx: AppContext) -> None:
    stored = json.loads((REPO / "docs" / "api" / "openapi.json").read_text(encoding="utf-8"))
    assert create_app(ctx).openapi() == stored, "run `uv run cyp dev openapi` (and web gen)"


@pytest.fixture
def two(data: Path, tmp_path: Path) -> Iterator[TestClient]:
    """Default context + a second profile 'dad' backed by a copy of the same store."""
    import shutil

    other = tmp_path / "dad-data"
    shutil.copytree(data, other)
    default = make_ctx(data)
    dad = make_ctx(other)

    def factory(slug: str) -> AppContext:
        if slug != "dad":
            raise ConfigError(f"profile {slug!r} not found")
        return dad

    reg = ProfileRegistry(default, factory=factory)
    with TestClient(create_app(default, registry=reg)) as cl:
        cl.default_ctx, cl.dad_ctx = default, dad  # type: ignore[attr-defined]
        yield cl
    default.close()
    dad.close()


def test_profile_header_routes_requests(two: TestClient) -> None:
    assert two.get("/v1/meta").status_code == 200
    assert two.get("/v1/meta", headers={PROFILE_HEADER: "dad"}).status_code == 200
    missing = two.get("/v1/meta", headers={PROFILE_HEADER: "nobody"})
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    etag_a = two.get("/v1/meta").headers["ETag"]
    etag_b = two.get("/v1/meta", headers={PROFILE_HEADER: "dad"}).headers["ETag"]
    assert etag_a != etag_b  # caches never mix athletes


def test_autopilot_write_is_gated(two: TestClient) -> None:
    h = {PROFILE_HEADER: "dad"}
    assert two.post("/v1/autopilot", json={"write": True}, headers=h).status_code == 422
    r = two.post("/v1/autopilot", json={"write": True, "confirm": True}, headers=h)
    assert r.status_code == 409 and "propose" in r.json()["detail"]
    dad: AppContext = two.dad_ctx  # type: ignore[attr-defined]
    set_planner_mode(Path(dad.athlete_config_path), "apply")
    dad.settings = dad.settings.model_copy(update={"cyp_features": "api.calendar_write=off"})
    r = two.post("/v1/autopilot", json={"write": True, "confirm": True}, headers=h)
    assert r.status_code == 403


def test_ride_log_and_runs(two: TestClient) -> None:
    page = two.get("/v1/activities", params={"rides_only": True, "limit": 1}).json()
    aid = page["items"][0]["id"]
    log = two.get(f"/v1/activities/{aid}/ride-log").json()
    assert log["workout"].startswith("📋 WORKOUT") and "⚙️ RIDE.LOG" in log["ride_log"]
    assert "STATUS :" in log["ride_log"] and log["text"].count("\n\n") == 1
    assert two.get("/v1/activities/999999/ride-log").status_code == 404
    assert two.get("/v1/autopilot/runs").json() == []  # empty until a run happened
