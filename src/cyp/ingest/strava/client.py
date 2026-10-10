"""Rate-limit-aware Strava API v3 client (httpx port of ``strava-analyis/api/client.py``).

Retry policy:
- HTTP 429: sleep to the next 15-minute clock boundary (or ``Retry-After``) plus a buffer and
  retry, capped. With ``wait_on_rate_limit=False`` a 429 raises :class:`RateLimitError`
  immediately so the caller can persist its cursor and exit (``cyp sync strava --no-wait``).
- HTTP 401: force one token refresh and retry exactly once.
- HTTP 5xx: exponential backoff, capped.
- Other 4xx: :class:`IngestError` with status and path (never the response body of a token
  endpoint).

Every response's ``X-RateLimit-Usage`` / ``X-RateLimit-Limit`` (and the read-only variants) are
parsed into :attr:`StravaClient.rate_limit` for budgeting and the ``job_runs`` snapshot.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from cyp.core.errors import IngestError, RateLimitError
from cyp.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://www.strava.com/api/v3"

DEFAULT_STREAM_KEYS: tuple[str, ...] = (
    "time",
    "distance",
    "latlng",
    "altitude",
    "velocity_smooth",
    "heartrate",
    "cadence",
    "watts",
    "temp",
    "moving",
    "grade_smooth",
)

MAX_RATE_LIMIT_RETRIES = 3
MAX_SERVER_ERROR_RETRIES = 3
RATE_LIMIT_BUFFER_S = 5.0
MIN_RATE_LIMIT_SLEEP_S = 1.0
WINDOW_S = 15 * 60
#: Strava caps ``per_page`` at 200 on list endpoints.
MAX_PER_PAGE = 200
#: Default short-window limits (overall / read) when no header has been seen yet.
DEFAULT_LIMIT_15M = 200
DEFAULT_READ_LIMIT_15M = 100


def seconds_until_next_window(now: float | None = None) -> float:
    """Seconds from ``now`` until the next 15-minute boundary (:00/:15/:30/:45); 0 on a boundary."""
    ts = time.time() if now is None else now
    when = dt.datetime.fromtimestamp(ts, tz=dt.UTC)
    into = (when.minute % 15) * 60 + when.second + when.microsecond / 1e6
    return (WINDOW_S - into) % WINDOW_S


def parse_pair(value: str | None) -> tuple[int, int] | None:
    """Parse a ``"short,long"`` rate-limit header into ints; ``None`` when absent/malformed."""
    if not value:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None


@dataclass
class RateLimitState:
    """Last-seen Strava rate-limit headers (``(15-min, daily)`` tuples)."""

    usage: tuple[int, int] | None = None
    limit: tuple[int, int] | None = None
    read_usage: tuple[int, int] | None = None
    read_limit: tuple[int, int] | None = None
    observed_at: float | None = None
    requests_made: int = 0
    waits: list[float] = field(default_factory=list)
    #: Set after sleeping through a window boundary; cleared by the next header observation.
    assume_reset: bool = False

    def record(self, headers: httpx.Headers, now: float | None = None) -> None:
        """Update from response headers (missing headers leave fields unchanged)."""
        for attr, name in (
            ("usage", "X-RateLimit-Usage"),
            ("limit", "X-RateLimit-Limit"),
            ("read_usage", "X-ReadRateLimit-Usage"),
            ("read_limit", "X-ReadRateLimit-Limit"),
        ):
            pair = parse_pair(headers.get(name))
            if pair is not None:
                setattr(self, attr, pair)
                self.observed_at = time.time() if now is None else now
                self.assume_reset = False

    def remaining_15m(self, now: float | None = None) -> int | None:
        """Reads left in the current 15-min window (min of overall and read quotas).

        Returns ``None`` before any header has been seen; returns the full limit when the
        observation predates the current window (the quota has reset since).
        """
        if self.usage is None:
            return None
        ts = time.time() if now is None else now
        reset = self.observed_at is not None and _window_start(ts) > self.observed_at
        if reset or self.assume_reset:
            return min(
                (self.limit or (DEFAULT_LIMIT_15M, 0))[0],
                (self.read_limit or (DEFAULT_READ_LIMIT_15M, 0))[0],
            )
        overall = (self.limit or (DEFAULT_LIMIT_15M, 0))[0] - self.usage[0]
        if self.read_usage is not None:
            read = (self.read_limit or (DEFAULT_READ_LIMIT_15M, 0))[0] - self.read_usage[0]
            return max(0, min(overall, read))
        return max(0, overall)

    def wait_needed_s(self, now: float | None = None) -> float:
        """Seconds to sleep before the next read: 0 while budget remains, else until next window."""
        remaining = self.remaining_15m(now)
        if remaining is None or remaining > 0:
            return 0.0
        return seconds_until_next_window(now) + RATE_LIMIT_BUFFER_S

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """JSON-serialisable summary for ``job_runs.rate_limit_snapshot`` / ``cyp doctor``."""
        return {
            "usage_15m": self.usage[0] if self.usage else None,
            "usage_daily": self.usage[1] if self.usage else None,
            "limit_15m": self.limit[0] if self.limit else None,
            "limit_daily": self.limit[1] if self.limit else None,
            "read_usage_15m": self.read_usage[0] if self.read_usage else None,
            "read_limit_15m": self.read_limit[0] if self.read_limit else None,
            "remaining_15m": self.remaining_15m(now),
            "observed_at": self.observed_at,
            "requests_made": self.requests_made,
            "waits_s": [round(w, 1) for w in self.waits],
        }


def _window_start(ts: float) -> float:
    return ts - (WINDOW_S - seconds_until_next_window(ts)) % WINDOW_S


class StravaClient:
    """Thin HTTP client; token handling is injected (see ``oauth.StravaAuth``).

    Args:
        token_provider: returns a valid bearer token (may refresh transparently).
        force_refresh: returns a freshly refreshed token after a 401.
        http: optional ``httpx.Client`` (tests inject a respx-mocked one).
        sleep: sleep function (tests inject a recorder).
        wait_on_rate_limit: ``False`` turns 429/budget exhaustion into :class:`RateLimitError`.
        allow_write: gate for the only mutating call, :meth:`update_activity_description`.
    """

    def __init__(
        self,
        token_provider: Callable[[], str],
        force_refresh: Callable[[], str] | None = None,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wait_on_rate_limit: bool = True,
        allow_write: bool = False,
        base_url: str = BASE_URL,
    ) -> None:
        self._token_provider = token_provider
        self._force_refresh = force_refresh or token_provider
        self._http = http or httpx.Client(timeout=60)
        self._sleep = sleep
        self.wait_on_rate_limit = wait_on_rate_limit
        self.allow_write = allow_write
        self.base_url = base_url.rstrip("/")
        self.rate_limit = RateLimitState()

    # -- core request handling --------------------------------------------------------------
    def _wait_or_raise(self, seconds: float, *, reason: str) -> None:
        seconds = max(seconds, MIN_RATE_LIMIT_SLEEP_S)
        if not self.wait_on_rate_limit:
            raise RateLimitError(
                f"Strava rate limit {reason}; next window in {seconds:.0f}s "
                f"(usage={self.rate_limit.usage}, limit={self.rate_limit.limit})",
                retry_after_s=seconds,
            )
        log.info("strava.rate_limit.wait", seconds=round(seconds, 1), reason=reason)
        self.rate_limit.waits.append(seconds)
        self._sleep(seconds)
        self.rate_limit.assume_reset = True

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}{path}"
        rate_retries = 0
        server_retries = 0
        auth_retried = False
        forced_token: str | None = None
        while True:
            # Proactive budgeting: if the last headers say the window is spent, wait first.
            pre_wait = self.rate_limit.wait_needed_s()
            if pre_wait > 0:
                self._wait_or_raise(pre_wait, reason="budget exhausted")
            token = forced_token if forced_token is not None else self._token_provider()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if server_retries >= MAX_SERVER_ERROR_RETRIES:
                    raise IngestError(f"Strava request failed for {path}: {exc}") from exc
                server_retries += 1
                self._sleep(2.0**server_retries)
                continue
            self.rate_limit.requests_made += 1
            self.rate_limit.record(resp.headers)

            if resp.status_code == 429:
                if rate_retries >= MAX_RATE_LIMIT_RETRIES:
                    raise RateLimitError(
                        "Strava rate limit exceeded and max retries reached "
                        f"(usage={self.rate_limit.usage}, limit={self.rate_limit.limit})"
                    )
                rate_retries += 1
                retry_after = _retry_after_s(resp)
                wait = (
                    retry_after + RATE_LIMIT_BUFFER_S
                    if retry_after is not None
                    else seconds_until_next_window() + RATE_LIMIT_BUFFER_S
                )
                self._wait_or_raise(wait, reason="429")
                continue

            if resp.status_code == 401:
                if auth_retried:
                    raise IngestError(
                        f"Strava rejected the token after refresh ({path}); "
                        "re-run `cyp auth strava`"
                    )
                auth_retried = True
                forced_token = self._force_refresh()
                continue

            if 500 <= resp.status_code < 600:
                if server_retries >= MAX_SERVER_ERROR_RETRIES:
                    raise IngestError(
                        f"Strava server error {resp.status_code} after {server_retries} retries"
                    )
                server_retries += 1
                self._sleep(2.0**server_retries)
                continue

            if 400 <= resp.status_code < 500:
                raise IngestError(
                    f"Strava {resp.status_code} for {method} {path}: {resp.text[:300]}",
                    status=resp.status_code,
                )

            return resp

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    # -- endpoints --------------------------------------------------------------------------
    def get_athlete(self) -> dict[str, Any]:
        """``GET /athlete``."""
        data = self._get_json("/athlete")
        return data if isinstance(data, dict) else {}

    def get_zones(self) -> dict[str, Any]:
        """``GET /athlete/zones`` (athlete HR/power zone definitions)."""
        data = self._get_json("/athlete/zones")
        return data if isinstance(data, dict) else {}

    def list_activities(
        self,
        *,
        after: int | None = None,
        before: int | None = None,
        page: int = 1,
        per_page: int = MAX_PER_PAGE,
    ) -> list[dict[str, Any]]:
        """One page of ``GET /athlete/activities`` (``after``/``before`` are epoch seconds)."""
        params: dict[str, Any] = {"page": page, "per_page": min(per_page, MAX_PER_PAGE)}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        data = self._get_json("/athlete/activities", params=params)
        return list(data) if isinstance(data, list) else []

    def iter_activities(
        self,
        *,
        after: int | None = None,
        before: int | None = None,
        per_page: int = MAX_PER_PAGE,
    ) -> Iterator[dict[str, Any]]:
        """Iterate every summary activity, paging until an empty page."""
        page = 1
        while True:
            batch = self.list_activities(after=after, before=before, page=page, per_page=per_page)
            if not batch:
                return
            yield from batch
            if len(batch) < min(per_page, MAX_PER_PAGE):
                return
            page += 1

    def get_activity(self, activity_id: int, *, include_all_efforts: bool = True) -> dict[str, Any]:
        """``GET /activities/{id}`` (detail incl. segment efforts, laps, gear)."""
        data = self._get_json(
            f"/activities/{activity_id}",
            params={"include_all_efforts": str(include_all_efforts).lower()},
        )
        return data if isinstance(data, dict) else {}

    def get_streams(
        self, activity_id: int, keys: tuple[str, ...] | list[str] | None = None
    ) -> dict[str, Any]:
        """``GET /activities/{id}/streams?key_by_type=true``."""
        keys = tuple(keys) if keys is not None else DEFAULT_STREAM_KEYS
        data = self._get_json(
            f"/activities/{activity_id}/streams",
            params={"keys": ",".join(keys), "key_by_type": "true"},
        )
        return data if isinstance(data, dict) else {}

    def get_activity_zones(self, activity_id: int) -> list[dict[str, Any]]:
        """``GET /activities/{id}/zones``."""
        data = self._get_json(f"/activities/{activity_id}/zones")
        return list(data) if isinstance(data, list) else []

    def get_laps(self, activity_id: int) -> list[dict[str, Any]]:
        """``GET /activities/{id}/laps``."""
        data = self._get_json(f"/activities/{activity_id}/laps")
        return list(data) if isinstance(data, list) else []

    def update_activity_description(self, activity_id: int, description: str) -> dict[str, Any]:
        """``PUT /activities/{id}`` with a new description (the ``RIDE.LOG`` footer target).

        Raises:
            IngestError: the client was constructed without ``allow_write=True``.
        """
        if not self.allow_write:
            raise IngestError("Strava writes are disabled (use StravaClient(allow_write=True))")
        resp = self._request("PUT", f"/activities/{activity_id}", data={"description": description})
        data = resp.json()
        return data if isinstance(data, dict) else {}


def _retry_after_s(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
