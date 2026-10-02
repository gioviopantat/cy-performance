"""`cyp auth strava`, `cyp sync strava` and the Strava block of `cyp doctor`."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from cyp import cli
from cyp.ingest.strava.client import StravaClient
from cyp.ingest.strava.oauth import TOKEN_URL
from cyp.store.db import make_engine, session_factory
from cyp.store.repo.sync_cursors import SyncCursorRepo
from tests.ingest.strava.conftest import FAKE_ACCESS, api, load_fixture, ok

runner = CliRunner()


def _env(data_dir: Path, db_url: str, **extra: str) -> dict[str, str]:
    return {
        "CYP_DATA_DIR": str(data_dir),
        "CYP_DB_URL": db_url,
        "INTERVALS_API_KEY": "fixture-icu-key",
        "STRAVA_CLIENT_ID": "12345",
        "STRAVA_CLIENT_SECRET": "fixture-client-secret",
        **extra,
    }


def _legacy_token(tmp_path: Path, *, expires_at: int) -> Path:
    src = tmp_path / "legacy-token.json"
    src.write_text(
        json.dumps(
            {
                "access_token": "legacy-access",
                "refresh_token": "legacy-refresh",
                "expires_at": expires_at,
                "scope": "activity:read_all,profile:read_all",
                "athlete_id": 188844906,
            }
        )
    )
    return src


def test_auth_strava_import_from(data_dir: Path, db_url: str, tmp_path: Path) -> None:
    env = _env(data_dir, db_url)
    src = _legacy_token(tmp_path, expires_at=int(time.time()) + 3600)
    result = runner.invoke(app := cli.app, ["auth", "strava", "--import-from", str(src)], env=env)
    assert result.exit_code == 0, result.output
    target = data_dir / "tokens" / "strava.json"
    assert target.is_file() and stat.S_IMODE(target.stat().st_mode) == 0o600
    assert "token saved to" in result.output and "athlete_id=188844906" in result.output
    assert "legacy-access" not in result.output and "legacy-refresh" not in result.output
    missing = runner.invoke(app, ["auth", "strava", "--import-from", str(tmp_path / "x")], env=env)
    assert missing.exit_code == 1 and "auth strava failed" in missing.output


def test_auth_strava_callback_flow_is_wired(
    data_dir: Path, db_url: str, monkeypatch: object, respx_mock: respx.MockRouter
) -> None:
    """The interactive flow is replaced by a stub; the command persists what it returns."""
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    captured: dict[str, object] = {}

    def fake_flow(auth: object, *, open_browser: bool, echo: object) -> object:
        captured["open_browser"] = open_browser
        from pydantic import SecretStr

        from cyp.ingest.strava.oauth import StravaAuth, StravaToken

        assert isinstance(auth, StravaAuth)
        tok = StravaToken(
            access_token=SecretStr("a"), refresh_token=SecretStr("r"), expires_at=5, athlete_id=1
        )
        auth.store.save(tok)
        return tok

    monkeypatch.setattr(cli, "run_local_callback_flow", fake_flow)
    result = runner.invoke(cli.app, ["auth", "strava", "--no-browser"], env=_env(data_dir, db_url))
    assert result.exit_code == 0, result.output
    assert captured == {"open_browser": False}
    assert (data_dir / "tokens" / "strava.json").is_file()


def test_sync_strava_disabled_and_no_subcommand(data_dir: Path, db_url: str) -> None:
    env = _env(data_dir, db_url, STRAVA_ENABLED="false")
    result = runner.invoke(cli.app, ["sync", "strava"], env=env)
    assert result.exit_code == 0 and "skipped (STRAVA_ENABLED=false)" in result.output
    bare = runner.invoke(cli.app, ["sync"], env=env)  # schema guard, no network
    assert bare.exit_code == 2 and "cyp db upgrade" in bare.output


def test_sync_strava_end_to_end_and_doctor(
    data_dir: Path, db_url: str, tmp_path: Path, athlete_yaml: Path, respx_mock: respx.MockRouter
) -> None:
    env = _env(data_dir, db_url)
    assert runner.invoke(cli.app, ["init"], env=env).exit_code == 0
    assert runner.invoke(cli.app, ["db", "upgrade"], env=env).exit_code == 0

    # doctor before auth: token missing is a warning, cursor unset
    before = runner.invoke(cli.app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert "Strava" in before.output
    assert "token: missing (run `cyp auth strava`)" in before.output
    assert "cursor activities_after: unset" in before.output
    assert "rate budget (last run): no snapshot yet" in before.output

    # expired token on disk -> the sync refreshes (and rotates) it first
    src = _legacy_token(tmp_path, expires_at=int(time.time()) - 5)
    assert (
        runner.invoke(cli.app, ["auth", "strava", "--import-from", str(src)], env=env).exit_code
        == 0
    )
    refresh = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("token_response.json"))
    )
    page = load_fixture("activities_page1.json")
    respx_mock.get(api("/athlete")).mock(return_value=ok(load_fixture("athlete.json")))
    respx_mock.get(api("/athlete/zones")).mock(return_value=ok(load_fixture("athlete_zones.json")))
    respx_mock.get(api("/athlete/activities")).mock(
        side_effect=lambda r: ok(page[:1] if r.url.params["page"] == "1" else [], usage="7,70")
    )
    respx_mock.get(api("/activities/1001")).mock(
        return_value=ok(load_fixture("activity_1001_detail.json"), usage="8,71")
    )
    respx_mock.get(api("/activities/1001/zones")).mock(
        return_value=ok(load_fixture("activity_1001_zones.json"), usage="9,72")
    )
    streams = respx_mock.get(api("/activities/1001/streams")).mock(return_value=ok({}))

    result = runner.invoke(cli.app, ["sync", "strava", "--max-detail", "5"], env=env)
    assert result.exit_code == 0, result.output
    assert refresh.called
    assert "details_fetched: 1" in result.output and "efforts: 2" in result.output
    assert "match: " in result.output and "rows_single_source=1" in result.output
    assert "cursor: None -> 1790632800" in result.output and "remaining_15m: 191" in result.output
    assert not streams.called  # fallback streams only with --streams
    assert "fixture-access-token-B" not in result.output
    bearer = respx_mock.calls.last.request.headers["Authorization"]
    assert bearer == "Bearer fixture-access-token-B"  # the rotated token was used
    on_disk = json.loads((data_dir / "tokens" / "strava.json").read_text())
    assert on_disk["refresh_token"] == "fixture-refresh-token-B"

    after = runner.invoke(cli.app, ["doctor", "--athlete-config", str(athlete_yaml)], env=env)
    assert "token: athlete_id=188844906" in after.output and "valid for" in after.output
    assert "cursor activities_after: 1790632800" in after.output
    assert "rate budget (last run): used 9/200 per 15 min, 191 remaining" in after.output
    assert "daily 72/2000" in after.output
    assert "sync:strava: ok" in after.output and "match: ok" in after.output
    assert "last unified sync: never" in after.output
    assert "fixture-" not in after.output

    engine = make_engine(db_url)
    with session_factory(engine)() as s:
        assert SyncCursorRepo(s).get("strava", "activities_after") == "1790632800"
    engine.dispose()


def test_sync_strava_no_wait_exit_code(
    data_dir: Path, db_url: str, tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    env = _env(data_dir, db_url)
    assert runner.invoke(cli.app, ["db", "upgrade"], env=env).exit_code == 0
    src = _legacy_token(tmp_path, expires_at=int(time.time()) + 3600)
    assert (
        runner.invoke(cli.app, ["auth", "strava", "--import-from", str(src)], env=env).exit_code
        == 0
    )
    respx_mock.get(api("/athlete")).mock(return_value=ok(load_fixture("athlete.json")))
    respx_mock.get(api("/athlete/zones")).mock(return_value=ok({}))
    respx_mock.get(api("/athlete/activities")).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "600"})
    )
    result = runner.invoke(cli.app, ["sync", "strava", "--no-wait"], env=env)
    assert result.exit_code == 2, result.output
    assert "stopped: rate limited" in result.output
    assert respx_mock.calls.last.request.headers["Authorization"] == "Bearer legacy-access"
    assert "legacy-access" not in result.output


def test_sync_strava_help_lists_options() -> None:
    result = runner.invoke(cli.app, ["sync", "strava", "--help"])
    assert result.exit_code == 0
    for opt in ("--full", "--streams", "--no-wait", "--max-detail"):
        assert opt in result.output
    assert isinstance(StravaClient, type)  # import sanity
    assert FAKE_ACCESS  # fixture constant is in use across the suite
