"""Strava OAuth2: token file store, refresh with atomic rotation, local-callback consent flow.

Tokens live in ``{CYP_DATA_DIR}/tokens/strava.json`` (mode 600) with the fields
``access_token``, ``refresh_token``, ``expires_at`` (epoch int), ``scope`` and ``athlete_id``;
the same shape ``strava-analyis`` used, so an existing ``token.json`` can be imported as-is.

Strava rotates the refresh token on every refresh, so :meth:`TokenStore.save` writes to a
temp file and ``os.replace`` moves it over the target: a crash mid-write never leaves a
half-written file, and the previous refresh token is never lost before the new one is on disk.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cyp.core.errors import ConfigError, IngestError
from cyp.logging import get_logger
from cyp.settings import Settings

log = get_logger(__name__)

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
TOKEN_FILENAME = "strava.json"

#: Refresh when the access token is within this many seconds of expiry.
REFRESH_LEEWAY_S = 120
#: Keys a token file must carry to be usable (``athlete_id`` is optional metadata).
REQUIRED_TOKEN_KEYS: frozenset[str] = frozenset({"access_token", "refresh_token", "expires_at"})
#: How long the local callback server waits for the browser redirect.
CALLBACK_TIMEOUT_S = 180


class StravaToken(BaseModel):
    """Persisted token set. Secrets are ``SecretStr`` so they never appear in logs/repr."""

    model_config = ConfigDict(extra="ignore")

    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: int = Field(ge=0)
    scope: str | None = None
    athlete_id: int | None = None

    def expires_in_s(self, now: float | None = None) -> float:
        """Seconds until expiry (negative when already expired)."""
        return self.expires_at - (time.time() if now is None else now)

    def needs_refresh(self, now: float | None = None, *, leeway_s: int = REFRESH_LEEWAY_S) -> bool:
        """True when the access token expires within ``leeway_s``."""
        return self.expires_in_s(now) <= leeway_s

    def to_file_dict(self) -> dict[str, Any]:
        """Plain dict with secret values revealed, for writing the token file only."""
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value(),
            "expires_at": self.expires_at,
            "scope": self.scope,
            "athlete_id": self.athlete_id,
        }


def normalize_token_response(
    data: dict[str, Any], *, fallback: StravaToken | None = None
) -> StravaToken:
    """Map a raw token-endpoint response onto :class:`StravaToken`.

    ``refresh_token``/``scope``/``athlete_id`` fall back to the previous token when the response
    omits them (Strava always returns a refresh token, but be defensive).

    Raises:
        IngestError: the response lacks an ``access_token`` or any expiry information.
    """
    access = data.get("access_token")
    if not access:
        raise IngestError("Strava token response missing 'access_token'; re-run `cyp auth strava`")
    refresh = data.get("refresh_token") or (
        fallback.refresh_token.get_secret_value() if fallback else None
    )
    if not refresh:
        raise IngestError("Strava token response missing 'refresh_token'; re-run `cyp auth strava`")
    if data.get("expires_at") is not None:
        expires_at = int(data["expires_at"])
    elif data.get("expires_in") is not None:
        expires_at = int(time.time()) + int(data["expires_in"])
    else:
        raise IngestError("Strava token response missing 'expires_at'/'expires_in'")
    athlete = data.get("athlete") or {}
    athlete_id = athlete.get("id", fallback.athlete_id if fallback else None)
    scope = data.get("scope", fallback.scope if fallback else None)
    return StravaToken(
        access_token=SecretStr(str(access)),
        refresh_token=SecretStr(str(refresh)),
        expires_at=expires_at,
        scope=scope,
        athlete_id=athlete_id,
    )


class TokenStore:
    """Load/save the Strava token file with 0600 permissions and atomic replacement."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenStore:
        """Store at ``{tokens_dir}/strava.json``."""
        return cls(settings.tokens_dir / TOKEN_FILENAME)

    def exists(self) -> bool:
        """True if a token file is present."""
        return self.path.is_file()

    def load(self) -> StravaToken | None:
        """Parse the token file; ``None`` when absent.

        Raises:
            ConfigError: the file exists but is not valid JSON / lacks required keys.
        """
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid JSON in {self.path}") from exc
        return _validate_token_dict(data, origin=self.path)

    def save(self, token: StravaToken) -> Path:
        """Atomically write ``token`` with mode 600; returns the path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(token.to_file_dict(), fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(self.path, 0o600)
        return self.path

    def delete(self) -> bool:
        """Remove the token file; returns whether it existed."""
        if self.path.is_file():
            self.path.unlink()
            return True
        return False


def _validate_token_dict(data: object, *, origin: Path) -> StravaToken:
    if not isinstance(data, dict):
        raise ConfigError(f"{origin} must contain a JSON object")
    missing = REQUIRED_TOKEN_KEYS - set(data)
    if missing:
        raise ConfigError(f"{origin} is missing token keys: {sorted(missing)}")
    try:
        return StravaToken.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"{origin} has an invalid token shape: {exc}") from exc


def import_token_file(source: Path, store: TokenStore) -> StravaToken:
    """Copy an existing ``strava-analyis`` ``token.json`` into ``store`` (validated, mode 600).

    Only the known keys are carried over; the source file is left untouched.

    Raises:
        ConfigError: source missing or malformed.
    """
    source = Path(source)
    if not source.is_file():
        raise ConfigError(f"token file not found: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {source}") from exc
    token = _validate_token_dict(data, origin=source)
    store.save(token)
    log.info("strava.token.imported", source=str(source), target=str(store.path))
    return token


def token_status(store: TokenStore, now: float | None = None) -> dict[str, Any]:
    """Non-secret summary of the stored token for ``cyp doctor``."""
    try:
        token = store.load()
    except ConfigError as exc:
        return {"present": True, "valid": False, "error": str(exc), "path": str(store.path)}
    if token is None:
        return {"present": False, "valid": False, "path": str(store.path)}
    remaining = token.expires_in_s(now)
    return {
        "present": True,
        "valid": True,
        "path": str(store.path),
        "athlete_id": token.athlete_id,
        "scope": token.scope,
        "expires_at": token.expires_at,
        "expires_in_s": int(remaining),
        "expired": remaining <= 0,
    }


def authorization_url(settings: Settings, *, state: str | None = None) -> str:
    """Build the Strava consent URL for the configured app/scope."""
    params = {
        "client_id": settings.strava_client_id,
        "redirect_uri": settings.strava_redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": settings.strava_scope,
    }
    if state:
        params["state"] = state
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


class StravaAuth:
    """Token provider for :class:`StravaClient`: loads, refreshes and persists tokens."""

    def __init__(
        self,
        settings: Settings,
        store: TokenStore | None = None,
        *,
        http: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or TokenStore.from_settings(settings)
        self._http = http or httpx.Client(timeout=30)
        self._lock = threading.Lock()
        self._cached: StravaToken | None = None

    # -- credentials ----------------------------------------------------------------
    def _require_credentials(self) -> tuple[str, str]:
        cid = self.settings.strava_client_id
        secret = self.settings.strava_client_secret.get_secret_value()
        if not cid or not secret:
            raise ConfigError("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET are not set")
        return cid, secret

    def _post_token(self, payload: dict[str, str]) -> dict[str, Any]:
        cid, secret = self._require_credentials()
        body = {"client_id": cid, "client_secret": secret, **payload}
        try:
            resp = self._http.post(TOKEN_URL, data=body)
        except httpx.HTTPError as exc:
            raise IngestError(f"Strava token request failed: {exc}") from exc
        if resp.status_code >= 400:
            # Never echo the body verbatim: Strava includes the offending field values.
            raise IngestError(
                f"Strava token request failed ({resp.status_code}); "
                "check client credentials or re-run `cyp auth strava`"
            )
        data = resp.json()
        if not isinstance(data, dict):
            raise IngestError("Strava token response was not a JSON object")
        return data

    # -- flows ----------------------------------------------------------------------
    def exchange_code(self, code: str) -> StravaToken:
        """Exchange an authorization code for tokens and persist them."""
        data = self._post_token({"code": code, "grant_type": "authorization_code"})
        token = normalize_token_response(data)
        self.store.save(token)
        self._cached = token
        log.info("strava.token.exchanged", athlete_id=token.athlete_id, scope=token.scope)
        return token

    def refresh(self, token: StravaToken) -> StravaToken:
        """Refresh ``token`` and atomically rotate the stored refresh token."""
        data = self._post_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token.get_secret_value(),
            }
        )
        new = normalize_token_response(data, fallback=token)
        self.store.save(new)
        self._cached = new
        log.info("strava.token.refreshed", expires_at=new.expires_at)
        return new

    def current(self) -> StravaToken:
        """The stored token (cached after first load).

        Raises:
            ConfigError: no token file; run ``cyp auth strava``.
        """
        if self._cached is None:
            token = self.store.load()
            if token is None:
                raise ConfigError(
                    f"no Strava token at {self.store.path}; run `cyp auth strava` "
                    "(or `cyp auth strava --import-from PATH`)"
                )
            self._cached = token
        return self._cached

    def get_valid_access_token(self, *, force: bool = False) -> str:
        """Access token, refreshed first when expiring within the leeway (or ``force``)."""
        with self._lock:
            token = self.current()
            if force or token.needs_refresh():
                token = self.refresh(token)
            return token.access_token.get_secret_value()

    def force_refresh_access_token(self) -> str:
        """Unconditional refresh; used by the client after a 401 mid-run."""
        return self.get_valid_access_token(force=True)


# ------------------------------------------------------------------------- local callback flow


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ``code`` / ``error`` from the redirect query string."""

    result: ClassVar[dict[str, str | None]] = {}
    expected_path: ClassVar[str] = "/callback"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != type(self).expected_path:
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        error = params.get("error", [None])[0]
        type(self).result = {"code": code, "error": error}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body><h2>Strava authorization received.</h2>"
            "<p>You can close this tab and return to the terminal.</p></body></html>"
            if code
            else "<html><body><h2>Authorization failed.</h2>"
            f"<p>{error or 'No code returned.'}</p></body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


def parse_redirect_uri(uri: str) -> tuple[str, int, str]:
    """``(host, port, path)`` from the redirect URI (defaults ``localhost:8721/callback``)."""
    parsed = urlparse(uri)
    return parsed.hostname or "localhost", parsed.port or 8721, parsed.path or "/callback"


def code_from_redirect_url(raw: str) -> str | None:
    """Extract ``code`` from a pasted redirect URL (manual fallback)."""
    raw = raw.strip()
    if not raw:
        return None
    return parse_qs(urlparse(raw).query).get("code", [None])[0]


def capture_code_via_server(host: str, port: int, path: str, *, timeout_s: float) -> str | None:
    """Serve one request on ``host:port`` and return the captured ``code`` (or ``None``)."""
    handler = _CallbackHandler
    handler.result = {}
    handler.expected_path = path
    server = HTTPServer((host, port), handler)
    server.timeout = timeout_s
    try:
        deadline = time.monotonic() + timeout_s
        while not handler.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if handler.result.get("error"):
        log.warning("strava.oauth.denied", error=handler.result["error"])
        return None
    return handler.result.get("code")


def run_local_callback_flow(
    auth: StravaAuth,
    *,
    open_browser: bool = True,
    timeout_s: float = CALLBACK_TIMEOUT_S,
    echo: Callable[[str], None] = print,
    prompt: Callable[[str], str] | None = input,
) -> StravaToken:
    """Interactive consent: open the authorize URL, catch the redirect locally, exchange the code.

    Falls back to prompting for the pasted redirect URL when the local server does not receive
    the callback (``prompt=None`` disables the fallback).

    Raises:
        IngestError: no authorization code was obtained.
    """
    auth._require_credentials()
    host, port, path = parse_redirect_uri(auth.settings.strava_redirect_uri)
    url = authorization_url(auth.settings)
    echo("Opening the Strava authorization page in your browser.")
    echo("If it does not open automatically, visit this URL manually:\n")
    echo(url + "\n")
    echo(f"Listening for the redirect on http://{host}:{port}{path} (up to {int(timeout_s)}s)...")
    if open_browser:
        with contextlib.suppress(Exception):  # browser launch is best-effort
            webbrowser.open(url)
    code: str | None = None
    try:
        code = capture_code_via_server(host, port, path, timeout_s=timeout_s)
    except OSError as exc:
        echo(f"Could not start the local callback server ({exc}).")
    if not code and prompt is not None:
        echo(
            "\nDid not capture the redirect automatically. After approving in the browser you "
            f"land on a URL like {auth.settings.strava_redirect_uri}?state=&code=...&scope=..."
        )
        code = code_from_redirect_url(prompt("Paste that full redirect URL here: "))
    if not code:
        raise IngestError("no Strava authorization code obtained")
    return auth.exchange_code(code)
