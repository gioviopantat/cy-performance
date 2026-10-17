"""Server-level settings, bind refusal, Host check and the ETag fingerprint (ADR-0009)."""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from cyp.api.app import create_app
from cyp.cli import app
from cyp.profiles import FilesystemProfileStore, ProfileMeta
from cyp.services.context import AppContext
from cyp.settings import get_settings


def test_server_settings_survive_profile_hermeticity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 2026-10-08: CYP_API_TOKEN was dropped once profiles existed."""
    store = FilesystemProfileStore(tmp_path / "profiles")
    store.create(
        ProfileMeta(slug="p1", display_name="p1", icu_athlete_id="i1", created=dt.date.today()),
        env={"INTERVALS_API_KEY": "k"},
        athlete_yaml="{}\n",
    )
    monkeypatch.setenv("CYP_API_TOKEN", "server-secret")
    monkeypatch.setenv("CYP_API_CORS_ORIGINS", "http://a.test")
    monkeypatch.setenv("CYP_TIMEZONE", "Europe/Paris")  # athlete-level: must not leak
    s = get_settings(cyp_profiles_dir=tmp_path / "profiles", cyp_profile="p1")
    assert s.cyp_api_token.get_secret_value() == "server-secret"
    assert s.api_cors_origins == ["http://a.test"]
    assert s.cyp_timezone == "Asia/Taipei"


def test_profile_env_is_not_interpolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FilesystemProfileStore(tmp_path / "profiles")
    store.create(
        ProfileMeta(slug="p1", display_name="p1", icu_athlete_id="i1", created=dt.date.today()),
        env={"INTERVALS_API_KEY": "${HOME}#x y"},
        athlete_yaml="{}\n",
    )
    s = get_settings(cyp_profiles_dir=tmp_path / "profiles", cyp_profile="p1")
    assert s.intervals_api_key.get_secret_value() == "${HOME}#x y"


def test_serve_refuses_a_public_bind_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CYP_API_TOKEN", raising=False)
    res = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])
    assert res.exit_code == 2 and "CYP_API_TOKEN" in res.output
    os.environ.pop("CYP_SERVE_HOST", None)


def test_loopback_without_token_checks_the_host_header(ctx: AppContext) -> None:
    api = create_app(ctx, trusted_hosts=["localhost", "127.0.0.1"])
    with TestClient(api, base_url="http://127.0.0.1:8765") as ok:
        assert ok.get("/healthz").status_code == 200
    with TestClient(api, base_url="http://rebind.attacker.test") as evil:
        assert evil.get("/v1/meta").status_code == 400
        assert evil.post("/v1/autopilot", json={"write": True, "confirm": True}).status_code == 400


def test_etag_changes_with_athlete_yaml_and_skips_feedback(
    client: TestClient, ctx: AppContext
) -> None:
    first = client.get("/v1/season").headers.get("ETag")
    path = Path(ctx.athlete_config_path)
    later = time.time() + 5
    os.utime(path, (later, later))
    assert client.get("/v1/season").headers.get("ETag") != first
    aid = client.get("/v1/activities").json()["items"][0]["id"]
    fb = client.get(f"/v1/activities/{aid}/feedback")
    assert "etag" not in {k.lower() for k in fb.headers}


def test_plan_commit_waits_for_no_run(client: TestClient, ctx: AppContext) -> None:
    """Review 2026-10-08: a UI commit could interleave with the 05:30 autopilot."""
    from cyp.jobs.runner import profile_lock

    with profile_lock(ctx.settings.cyp_data_dir):
        r = client.post("/v1/plan/commit")
    assert r.status_code == 409


def test_jobs_are_listed_per_profile() -> None:
    from cyp.services.jobs import JobManager

    j = JobManager()
    try:
        j.submit("sync", lambda: None, profile="a")
        j.submit("sync", lambda: None, profile="b")
        j.wait()
        assert [x.profile for x in j.list(profile="a")] == ["a"] and len(j.list()) == 2
    finally:
        j.shutdown()
