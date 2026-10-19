"""Token store (mode 600, atomic rotation), refresh, import and the callback helpers."""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import SecretStr

from cyp.core.errors import ConfigError, IngestError
from cyp.ingest.strava.oauth import (
    TOKEN_URL,
    StravaAuth,
    StravaToken,
    TokenStore,
    authorization_url,
    capture_code_via_server,
    code_from_redirect_url,
    import_token_file,
    normalize_token_response,
    parse_redirect_uri,
    run_local_callback_flow,
    token_status,
)
from cyp.settings import Settings
from tests.ingest.strava.conftest import FAKE_ACCESS, FAKE_REFRESH, load_fixture


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_save_is_mode_600_and_roundtrips(token_store: TokenStore, valid_token: StravaToken) -> None:
    assert token_store.exists()
    assert _mode(token_store.path) == 0o600
    loaded = token_store.load()
    assert loaded is not None
    assert loaded.access_token.get_secret_value() == FAKE_ACCESS
    assert loaded.athlete_id == 90000001
    assert "fixture-" not in repr(loaded)  # SecretStr masks
    assert not token_store.path.with_name("strava.json.tmp").exists()


def test_load_rejects_bad_files(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.json")
    assert store.load() is None
    store.path.write_text("not json")
    with pytest.raises(ConfigError, match="invalid JSON"):
        store.load()
    store.path.write_text(json.dumps({"access_token": "x"}))
    with pytest.raises(ConfigError, match="missing token keys"):
        store.load()


def test_normalize_token_response_fallbacks(valid_token: StravaToken) -> None:
    data = load_fixture("token_response.json")
    tok = normalize_token_response(data)
    assert tok.expires_at == 1900000000
    assert tok.athlete_id == 90000001
    assert tok.refresh_token.get_secret_value() == "fixture-refresh-token-B"
    # scope/athlete fall back to the previous token; expires_in derives expires_at
    partial = {"access_token": "n", "refresh_token": "r", "expires_in": 100}
    tok2 = normalize_token_response(partial, fallback=valid_token)
    assert tok2.scope == valid_token.scope and tok2.athlete_id == valid_token.athlete_id
    assert tok2.expires_at >= int(time.time()) + 99
    with pytest.raises(IngestError, match="access_token"):
        normalize_token_response({"refresh_token": "r", "expires_at": 1})
    with pytest.raises(IngestError, match="refresh_token"):
        normalize_token_response({"access_token": "a", "expires_at": 1})


def test_refresh_rotates_refresh_token_atomically(
    strava_settings: Settings, token_store: TokenStore, respx_mock: respx.MockRouter
) -> None:
    expired = StravaToken(
        access_token=SecretStr(FAKE_ACCESS),
        refresh_token=SecretStr(FAKE_REFRESH),
        expires_at=int(time.time()) - 10,
        scope="activity:read_all",
        athlete_id=7,
    )
    token_store.save(expired)
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("token_response.json"))
    )
    auth = StravaAuth(strava_settings, token_store, http=httpx.Client())
    access = auth.get_valid_access_token()
    assert access == "fixture-access-token-B"
    assert route.called
    body = route.calls.last.request.content.decode()
    assert "grant_type=refresh_token" in body
    assert f"refresh_token={FAKE_REFRESH}" in body
    assert "client_secret=fixture-client-secret" in body
    on_disk = json.loads(token_store.path.read_text())
    assert on_disk["refresh_token"] == "fixture-refresh-token-B"  # rotated
    assert on_disk["expires_at"] == 1900000000
    assert _mode(token_store.path) == 0o600
    # second call: still valid, no network
    route.reset()
    assert auth.get_valid_access_token() == "fixture-access-token-B"
    assert not route.called
    # forced refresh hits the endpoint again
    auth.force_refresh_access_token()
    assert route.call_count == 1


def test_refresh_failure_does_not_leak_body(
    strava_settings: Settings, token_store: TokenStore, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"errors": [{"field": "refresh_token"}]})
    )
    auth = StravaAuth(strava_settings, token_store, http=httpx.Client())
    with pytest.raises(IngestError, match="400") as exc:
        auth.force_refresh_access_token()
    assert "refresh_token" not in str(exc.value)
    assert json.loads(token_store.path.read_text())["refresh_token"] == FAKE_REFRESH  # untouched


def test_missing_credentials_and_missing_token(strava_settings: Settings, tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "none.json")
    auth = StravaAuth(strava_settings, store, http=httpx.Client())
    with pytest.raises(ConfigError, match="cyp auth strava"):
        auth.get_valid_access_token()
    bare = Settings(strava_client_id="", cyp_data_dir=tmp_path)
    with pytest.raises(ConfigError, match="STRAVA_CLIENT_ID"):
        StravaAuth(bare, store, http=httpx.Client()).exchange_code("abc")


def test_exchange_code(
    strava_settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("token_response.json"))
    )
    store = TokenStore(tmp_path / "tokens" / "strava.json")
    tok = StravaAuth(strava_settings, store, http=httpx.Client()).exchange_code("the-code")
    assert "code=the-code" in route.calls.last.request.content.decode()
    assert "grant_type=authorization_code" in route.calls.last.request.content.decode()
    assert tok.athlete_id == 90000001 and store.exists() and _mode(store.path) == 0o600


def test_import_token_file(tmp_path: Path, strava_settings: Settings) -> None:
    src = tmp_path / "legacy" / "token.json"
    src.parent.mkdir()
    src.write_text(
        json.dumps(
            {
                "access_token": "legacy-a",
                "refresh_token": "legacy-r",
                "expires_at": 1800000000,
                "scope": "read,activity:read_all",
                "athlete_id": 42,
                "extra_key": "ignored",
            }
        )
    )
    store = TokenStore.from_settings(strava_settings)
    tok = import_token_file(src, store)
    assert tok.athlete_id == 42
    saved = json.loads(store.path.read_text())
    assert set(saved) == {"access_token", "refresh_token", "expires_at", "scope", "athlete_id"}
    assert _mode(store.path) == 0o600
    with pytest.raises(ConfigError, match="not found"):
        import_token_file(tmp_path / "nope.json", store)
    src.write_text(json.dumps({"access_token": "only"}))
    with pytest.raises(ConfigError, match="missing token keys"):
        import_token_file(src, store)


def test_token_status(token_store: TokenStore, tmp_path: Path) -> None:
    status = token_status(token_store)
    assert status["present"] and status["valid"] and not status["expired"]
    assert status["athlete_id"] == 90000001
    assert FAKE_ACCESS not in json.dumps(status)
    assert token_status(TokenStore(tmp_path / "x.json")) == {
        "present": False,
        "valid": False,
        "path": str(tmp_path / "x.json"),
    }
    token_store.path.write_text("{}")
    assert token_status(token_store)["valid"] is False


def test_authorization_url_and_redirect_parsing(strava_settings: Settings) -> None:
    url = authorization_url(strava_settings, state="xyz")
    assert url.startswith("https://www.strava.com/oauth/authorize?")
    assert "client_id=12345" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8721%2Fcallback" in url
    assert "scope=activity%3Aread_all%2Cprofile%3Aread_all" in url
    assert "state=xyz" in url
    assert parse_redirect_uri("http://localhost:8721/callback") == ("localhost", 8721, "/callback")
    assert parse_redirect_uri("http://127.0.0.1/cb") == ("127.0.0.1", 8721, "/cb")
    assert (
        code_from_redirect_url("http://localhost:8721/callback?state=&code=abc&scope=read") == "abc"
    )
    assert code_from_redirect_url("") is None


def test_local_callback_server_captures_code(
    strava_settings: Settings, token_store: TokenStore, respx_mock: respx.MockRouter
) -> None:
    """Run the callback flow end-to-end on an ephemeral port; the browser is simulated."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings = strava_settings.model_copy(
        update={"strava_redirect_uri": f"http://127.0.0.1:{port}/callback"}
    )
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("token_response.json"))
    )
    # respx intercepts httpx only; the callback request is plain sockets via urllib.
    from urllib.request import urlopen

    def browser() -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/callback?state=&code=cb-code") as resp:
                    assert b"authorization received" in resp.read()
                return
            except OSError:
                time.sleep(0.05)

    thread = threading.Thread(target=browser, daemon=True)
    thread.start()
    echoed: list[str] = []
    auth = StravaAuth(settings, token_store, http=httpx.Client())
    tok = run_local_callback_flow(
        auth, open_browser=False, timeout_s=5, echo=echoed.append, prompt=None
    )
    thread.join(timeout=5)
    assert tok.access_token.get_secret_value() == "fixture-access-token-B"
    assert any("Listening for the redirect" in line for line in echoed)


def test_capture_code_times_out_and_prompt_fallback(
    strava_settings: Settings, token_store: TokenStore, respx_mock: respx.MockRouter
) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert capture_code_via_server("127.0.0.1", port, "/callback", timeout_s=0.2) is None
    settings = strava_settings.model_copy(
        update={"strava_redirect_uri": f"http://127.0.0.1:{port}/callback"}
    )
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=load_fixture("token_response.json"))
    )
    auth = StravaAuth(settings, token_store, http=httpx.Client())
    tok = run_local_callback_flow(
        auth,
        open_browser=False,
        timeout_s=0.2,
        echo=lambda _: None,
        prompt=lambda _: "http://127.0.0.1/callback?code=pasted",
    )
    assert tok.athlete_id == 90000001
    with pytest.raises(IngestError, match="no Strava authorization code"):
        run_local_callback_flow(
            auth, open_browser=False, timeout_s=0.2, echo=lambda _: None, prompt=lambda _: ""
        )
