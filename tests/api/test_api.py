"""HTTP API against a seeded synthetic store: contract, caching, errors, what-ifs, jobs."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from cyp.api.app import create_app
from cyp.services.context import AppContext
from cyp.store.models import AthleteSettingsHistory, PlannedWorkout
from tests.api.conftest import TODAY, make_ctx


def test_health_meta_and_openapi(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    meta = client.get("/v1/meta").json()
    assert meta["today"] == TODAY.isoformat()
    assert meta["athlete"]["ftp"] > 0 and meta["athlete"]["w_kg"] > 3
    assert meta["season"]["week_index"] == 1 and meta["season"]["weeks_total"] == 26
    assert meta["plan_mode"] == "propose"
    spec = client.get("/openapi.json").json()
    for path in (
        "/v1/ftp",
        "/v1/plan/preview",
        "/v1/activities/{activity_id}/streams",
        "/v1/jobs/{kind}",
    ):
        assert path in spec["paths"]


def test_etag_round_trip(client: TestClient) -> None:
    r1 = client.get("/v1/fitness")
    etag = r1.headers["etag"]
    assert r1.status_code == 200 and etag
    r2 = client.get("/v1/fitness", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    # A different query is a different resource.
    r3 = client.get("/v1/fitness?start=2026-09-01", headers={"If-None-Match": etag})
    assert r3.status_code == 200
    # Writing data changes the version -> the old ETag no longer matches.
    client.post("/v1/readiness/recompute", json={"dates": [TODAY.isoformat()]})
    assert client.get("/v1/fitness", headers={"If-None-Match": etag}).status_code == 200


def test_fitness_and_activities(client: TestClient) -> None:
    fit = client.get("/v1/fitness", params={"start": "2026-09-01", "end": "2026-10-05"}).json()
    assert len(fit["points"]) == 35 and fit["points"][-1]["ctl"] is not None
    assert (
        client.get("/v1/fitness", params={"start": "2026-10-05", "end": "2026-09-01"}).status_code
        == 422
    )
    page = client.get("/v1/activities", params={"rides_only": True, "limit": 5}).json()
    assert page["total"] > 20 and len(page["items"]) == 5
    first = page["items"][0]
    assert first["date"] >= page["items"][-1]["date"] and first["is_ride"]
    detail = client.get(f"/v1/activities/{first['id']}").json()
    assert detail["explanation"]["key"] == f"ride:{first['id']}" and detail["has_streams"]
    st = client.get(f"/v1/activities/{first['id']}/streams", params={"resolution_s": 30}).json()
    assert st["resolution_s"] == 30 and set(st["columns"]) >= {"t_s", "watts", "hr", "lat"}
    assert len(st["columns"]["watts"]) == st["n"]
    missing = client.get("/v1/activities/999999")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_ftp_status_what_if_and_accept(client: TestClient, ctx: AppContext) -> None:
    st = client.get("/v1/ftp").json()
    assert st["current_ftp"] and st["windows"]["42d"]["cp_2p"]["cp"] > 0
    assert st["estimate_source"] == "icu_eftp" and st["estimates"]
    assert st["compute_ms"] < 500
    low = client.post("/v1/ftp/what-if", json={"ftp": 200}).json()
    assert low["current_ftp"] == 200
    prop = low["proposal"]
    # Estimates sit far above 200 W; either proposed up or withheld for lack of a 20-min effort.
    assert prop["days_sustained"] >= 14 and (prop["direction"] == "up" or prop["unsupported"])
    assert client.post("/v1/ftp/what-if", json={"ftp": 10}).status_code == 422
    acc = client.post("/v1/ftp/accept", json={"ftp": 270, "effective_from": "2026-10-06"})
    assert acc.status_code == 200 and acc.json()["current_ftp"] == 270
    with ctx.factory() as s:
        rows = s.scalars(
            select(AthleteSettingsHistory).where(AthleteSettingsHistory.source == "manual")
        ).all()
        assert [r.ftp for r in rows] == [270.0]


def test_plan_preview_commit_and_stored(client: TestClient, ctx: AppContext) -> None:
    base = client.post("/v1/plan/preview", json={}).json()
    assert not base["persisted"] and base["days"][0]["date"] == TODAY.isoformat()
    assert len(base["days"]) == 14 and base["projection"] and base["weeks"]
    assert base["compute_ms"] < 1000
    hard = [d for d in base["days"] if d["role"] == "hit"]
    assert hard and hard[0]["steps"] and hard[0]["workout_text"]
    # What-if: Tuesday off -> no workout that day; nothing stored.
    off = client.post("/v1/plan/preview", json={"date_minutes": {"2026-10-06": 0}}).json()
    assert off["days"][0]["template_id"] is None
    rest = client.post("/v1/plan/preview", json={"readiness": "REST"}).json()
    assert rest["days"][0]["template_id"] is None and rest["adaptations"]
    assert client.post("/v1/plan/preview", json={"horizon_days": 99}).status_code == 422
    with ctx.factory() as s:
        assert s.scalars(select(PlannedWorkout)).first() is None
    committed = client.post("/v1/plan/commit").json()
    assert committed["persisted"] and committed["changes"]["created"]
    stored = client.get("/v1/plan").json()
    assert stored and stored[0]["date"] == TODAY.isoformat() and stored[0]["steps"]
    season = client.get("/v1/season").json()
    assert len(season["weeks"]) == 26 and season["weeks"][7]["checkpoint_ftp"] == 272


def test_readiness_trends_explain_reports(client: TestClient) -> None:
    on_the_fly = client.get(f"/v1/readiness/{TODAY}").json()
    assert on_the_fly["recommendation"] in {"REST", "EASY", "AS_PLANNED", "UPGRADE"}
    assert on_the_fly["components"] and on_the_fly["recommendation_zh"]
    client.post("/v1/readiness/recompute", json={"dates": ["2026-10-05", TODAY.isoformat()]})
    hist = client.get("/v1/readiness", params={"start": "2026-10-01"}).json()
    assert [r["date"] for r in hist] == ["2026-10-05", TODAY.isoformat()]
    assert client.get("/v1/trends/latest").status_code == 404
    rep = client.post("/v1/trends/recompute").json()
    assert rep["pmc_agreement"]["within_tolerance"]
    assert client.get("/v1/trends/latest").json()["as_of"] == TODAY.isoformat()
    ex = client.get("/v1/explain/ftp.proposal").json()
    assert ex["source"] == "reports/trends/latest.json"
    assert client.get(f"/v1/explain/readiness.{TODAY}").json()["source"] == "readiness_daily"
    assert client.get("/v1/explain/nope").status_code == 404
    built = client.post("/v1/reports/daily", params={"day": TODAY.isoformat()}).json()
    assert built["stem"] == TODAY.isoformat() and "# 每日報告" in built["markdown"]
    assert client.get("/v1/reports/daily").json() == [TODAY.isoformat()]
    assert client.get("/v1/reports/daily/../../etc").status_code == 404
    terms = client.get("/v1/glossary").json()
    assert "readiness_v1" in terms
    assert "準備度" in client.get("/v1/glossary/readiness_v1").text


def test_jobs(client: TestClient) -> None:
    assert client.post("/v1/jobs/sync").status_code == 409  # no API key configured
    job = client.post("/v1/jobs/analyze").json()
    assert job["status"] == "queued"
    client.app.state.jobs.wait()  # type: ignore[attr-defined]
    done = client.get(f"/v1/jobs/{job['id']}").json()
    assert done["status"] == "ok" and isinstance(done["result"], dict)
    assert client.get("/v1/jobs/unknown").status_code == 404
    assert [j["id"] for j in client.get("/v1/jobs").json()] == [job["id"]]


def test_empty_store_and_token(tmp_path: Path) -> None:
    from cyp.store.migrate import upgrade_head

    data = tmp_path / "data"
    upgrade_head(f"sqlite:///{data / 'cyp.sqlite'}")
    c = make_ctx(data, cyp_api_token="s3cret")
    with TestClient(create_app(c)) as cl:
        assert cl.get("/v1/meta").status_code == 401
        auth = {"Authorization": "Bearer s3cret"}
        meta = cl.get("/v1/meta", headers=auth).json()
        assert meta["athlete"] is None
        r = cl.get("/v1/ftp", headers=auth)
        assert r.status_code == 409 and r.json()["error"] == "no_data"
        assert cl.get("/healthz").status_code == 200  # probes stay open
    c.close()


def test_recompute_is_fast_when_cached(client: TestClient) -> None:
    """Interactive paths must stay well under human-noticeable latency once warm."""
    client.get("/v1/ftp")
    client.post("/v1/plan/preview", json={})
    ftp_ms = client.post("/v1/ftp/what-if", json={"ftp": 260}).json()["compute_ms"]
    plan_ms = client.post("/v1/plan/preview", json={"weekday_minutes": {"tue": 60}}).json()[
        "compute_ms"
    ]
    assert ftp_ms < 100, ftp_ms
    assert plan_ms < 300, plan_ms
    assert dt.date.fromisoformat(client.get("/v1/meta").json()["today"]) == TODAY
