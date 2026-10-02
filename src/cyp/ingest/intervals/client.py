"""httpx client for the intervals.icu API v1 (docs/02 §3).

Behaviour:
- auth via an :class:`~cyp.ingest.intervals.auth.AuthStrategy` (API key Basic auth in v1);
- browser-like ``User-Agent`` (Cloudflare has blocked default Python UAs);
- athlete id ``0`` (= key owner) is resolved to the real id via ``GET /athlete/0`` and cached;
- exponential backoff on 5xx (max :attr:`IntervalsClient.max_retries`), ``Retry-After`` honoured
  on 429, ``X-RateLimit-*`` headers recorded after every response;
- every method returns the parsed JSON (``dict`` / ``list``) so raw payloads can be stored
  verbatim ("raw first, derive later").
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from cyp.core.errors import IngestError, RateLimitError
from cyp.ingest.intervals.auth import AuthStrategy
from cyp.logging import get_logger

log = get_logger(__name__)

BASE_URL = "https://intervals.icu/api/v1"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36 cy-performance/0.1"
)

JsonDict = dict[str, Any]
JsonList = list[dict[str, Any]]
DateLike = dt.date | dt.datetime | str

#: Stream types we ask icu for; mapped onto the Parquet schema in :mod:`.streams`.
DEFAULT_STREAM_TYPES: tuple[str, ...] = (
    "time",
    "watts",
    "heartrate",
    "cadence",
    "velocity_smooth",
    "altitude",
    "latlng",
    "distance",
    "grade_smooth",
    "temp",
    "moving",
)

#: ``curves`` values for ``GET /athlete/{id}/power-curves`` keyed by our snapshot window name.
POWER_CURVE_WINDOWS: dict[str, str] = {"42d": "42d", "90d": "90d", "season": "s0"}

RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})


@dataclass
class RateLimitState:
    """Last seen ``X-RateLimit-*`` headers (docs/02 §3.1)."""

    limit: int | None = None
    remaining: int | None = None
    retry_after_s: float | None = None
    observed_at: str | None = None
    hits_429: int = 0

    def update(self, headers: Mapping[str, str]) -> None:
        """Record the headers of a response, if present."""
        changed = False
        for key, attr in (("x-ratelimit-limit", "limit"), ("x-ratelimit-remaining", "remaining")):
            raw = headers.get(key)
            if raw is not None and raw.strip().lstrip("-").isdigit():
                setattr(self, attr, int(raw))
                changed = True
        if changed:
            self.observed_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()

    def snapshot(self) -> dict[str, Any]:
        """Serialisable form for ``job_runs.rate_limit_snapshot``."""
        return {
            "intervals_limit": self.limit,
            "intervals_remaining": self.remaining,
            "intervals_retry_after_s": self.retry_after_s,
            "intervals_observed_at": self.observed_at,
            "intervals_429_hits": self.hits_429,
        }


@dataclass
class IntervalsClient:
    """Thin, typed wrapper over the intervals.icu REST API.

    ``athlete_id`` may be ``"0"`` (key owner); :meth:`resolve_athlete_id` replaces it with the
    real id on first use. ``sleep`` is injectable so tests never wait.
    """

    auth: AuthStrategy
    athlete_id: str = "0"
    base_url: str = BASE_URL
    timeout_s: float = 30.0
    max_retries: int = 3
    backoff_base_s: float = 0.5
    max_retry_after_s: float = 120.0
    sleep: Callable[[float], None] = time.sleep
    transport: httpx.BaseTransport | None = None
    rate_limit: RateLimitState = field(default_factory=RateLimitState)
    _http: httpx.Client | None = field(default=None, init=False, repr=False)
    _resolved_id: str | None = field(default=None, init=False, repr=False)
    _athlete_cache: JsonDict | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------ lifecycle

    @property
    def http(self) -> httpx.Client:
        """Lazily created ``httpx.Client`` with auth + UA headers."""
        if self._http is None:
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            headers.update(self.auth.headers())
            self._http = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_s,
                transport=self.transport,
            )
        return self._http

    def close(self) -> None:
        """Close the underlying connection pool."""
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> IntervalsClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ transport

    def request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        """Perform a request with retries; returns parsed JSON (``None`` for empty bodies).

        Raises:
            RateLimitError: 429 persisted beyond ``max_retries``.
            IngestError: non-retryable 4xx, 5xx after retries, or transport failure.
        """
        clean_params = _clean_params(params)
        attempt = 0
        while True:
            try:
                response = self.http.request(method, path, params=clean_params, json=json)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    raise IngestError(f"intervals.icu {method} {path}: {exc}") from exc
                self._backoff(attempt, reason=f"transport error: {exc}")
                attempt += 1
                continue

            self.rate_limit.update(response.headers)
            status = response.status_code

            if status == 429:
                self.rate_limit.hits_429 += 1
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                self.rate_limit.retry_after_s = delay
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"intervals.icu rate limit on {method} {path}", retry_after_s=delay
                    )
                wait = min(
                    delay if delay is not None else self._delay(attempt), self.max_retry_after_s
                )
                log.warning("intervals.429", path=path, wait_s=wait, attempt=attempt)
                self.sleep(wait)
                attempt += 1
                continue

            if status in RETRYABLE_STATUS:
                if attempt >= self.max_retries:
                    raise IngestError(
                        f"intervals.icu {method} {path} failed with {status} after "
                        f"{attempt + 1} attempts: {response.text[:200]}"
                    )
                self._backoff(attempt, reason=f"HTTP {status}")
                attempt += 1
                continue

            if status >= 400:
                raise IngestError(
                    f"intervals.icu {method} {path} -> HTTP {status}: {response.text[:300]}"
                )

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise IngestError(f"intervals.icu {method} {path}: invalid JSON body") from exc

    def get(self, path: str, **params: Any) -> Any:
        """``GET`` helper."""
        return self.request("GET", path, params=params)

    def _delay(self, attempt: int) -> float:
        return float(self.backoff_base_s * (2**attempt))

    def _backoff(self, attempt: int, *, reason: str) -> None:
        wait = self._delay(attempt)
        log.warning("intervals.retry", reason=reason, wait_s=wait, attempt=attempt)
        self.sleep(wait)

    # ------------------------------------------------------------------ athlete

    def resolve_athlete_id(self) -> str:
        """Real athlete id (``i123456``); fetches ``GET /athlete/0`` once when configured as 0."""
        if self._resolved_id is not None:
            return self._resolved_id
        if self.athlete_id and self.athlete_id != "0":
            self._resolved_id = self.athlete_id
            return self._resolved_id
        athlete = self.get_athlete()
        real = str(athlete.get("id") or "")
        if not real:
            raise IngestError("GET /athlete/0 returned no id; cannot resolve athlete")
        self._resolved_id = real
        log.info("intervals.athlete_resolved", athlete_id=real)
        return real

    @property
    def resolved_athlete_id(self) -> str | None:
        """Cached real id, or ``None`` until :meth:`resolve_athlete_id` has run."""
        return self._resolved_id

    def get_athlete(self) -> JsonDict:
        """``GET /athlete/{id}`` (includes ``sportSettings``). Cached per client instance."""
        if self._athlete_cache is None:
            data = self.get(f"/athlete/{self.athlete_id}")
            if not isinstance(data, dict):
                raise IngestError("GET /athlete returned a non-object body")
            self._athlete_cache = data
            if self._resolved_id is None and data.get("id"):
                self._resolved_id = str(data["id"])
        return self._athlete_cache

    def get_sport_settings(self) -> JsonList:
        """``GET /athlete/{id}/sport-settings``."""
        return _as_list(self.get(f"/athlete/{self.resolve_athlete_id()}/sport-settings"))

    # ------------------------------------------------------------------ activities

    def list_activities(
        self,
        oldest: DateLike,
        newest: DateLike | None = None,
        fields: Iterable[str] | None = None,
        *,
        limit: int | None = None,
    ) -> JsonList:
        """``GET /athlete/{id}/activities`` (newest first; local ISO dates)."""
        params: dict[str, Any] = {"oldest": _iso_local(oldest)}
        if newest is not None:
            params["newest"] = _iso_local(newest)
        if fields:
            params["fields"] = ",".join(fields)
        if limit is not None:
            params["limit"] = limit
        return _as_list(self.get(f"/athlete/{self.resolve_athlete_id()}/activities", **params))

    def get_activity(self, activity_id: str, *, intervals: bool = False) -> JsonDict:
        """``GET /activity/{id}`` (optionally with ``icu_intervals`` inline)."""
        params: dict[str, Any] = {}
        if intervals:
            params["intervals"] = "true"
        return _as_dict(self.get(f"/activity/{activity_id}", **params))

    def get_activity_intervals(self, activity_id: str) -> JsonDict:
        """``GET /activity/{id}/intervals`` -> ``{id, analyzed, icu_intervals, icu_groups}``."""
        return _as_dict(self.get(f"/activity/{activity_id}/intervals"))

    def get_activity_streams(
        self, activity_id: str, types: Iterable[str] = DEFAULT_STREAM_TYPES
    ) -> JsonList:
        """``GET /activity/{id}/streams.json?types=`` -> list of ``{type, name, data, ...}``.

        Empty for Strava-origin activities (icu does not serve their files).
        """
        return _as_list(self.get(f"/activity/{activity_id}/streams.json", types=",".join(types)))

    def get_activity_power_curve(self, activity_id: str) -> JsonDict:
        """``GET /activity/{id}/power-curve.json``."""
        return _as_dict(self.get(f"/activity/{activity_id}/power-curve.json"))

    # ------------------------------------------------------------------ curves / models

    def get_power_curves(
        self,
        type: str = "Ride",
        curves: Iterable[str] = tuple(POWER_CURVE_WINDOWS.values()),
        newest: DateLike | None = None,
    ) -> JsonDict:
        """``GET /athlete/{id}/power-curves.json?type=&curves=&newest=``.

        ``curves`` uses icu's grammar: ``42d``, ``90d``, ``s0`` (current season), ``1y``, ``all``.
        Returns ``{"list": [DataCurve, ...], "activities": {...}}``.
        """
        params: dict[str, Any] = {"type": type, "curves": ",".join(curves)}
        if newest is not None:
            params["newest"] = _iso_local(newest)
        return _as_dict(
            self.get(f"/athlete/{self.resolve_athlete_id()}/power-curves.json", **params)
        )

    def get_mmp_model(self, type: str = "Ride") -> JsonDict:
        """``GET /athlete/{id}/mmp-model?type=`` -> ``PowerModel`` (cp, w', pmax, ftp)."""
        return _as_dict(self.get(f"/athlete/{self.resolve_athlete_id()}/mmp-model", type=type))

    # ------------------------------------------------------------------ wellness / calendar

    def list_wellness(self, oldest: DateLike, newest: DateLike | None = None) -> JsonList:
        """``GET /athlete/{id}/wellness.json?oldest=&newest=`` (inclusive local dates)."""
        params: dict[str, Any] = {"oldest": _iso_date(oldest)}
        if newest is not None:
            params["newest"] = _iso_date(newest)
        return _as_list(self.get(f"/athlete/{self.resolve_athlete_id()}/wellness.json", **params))

    def list_events(
        self,
        oldest: DateLike,
        newest: DateLike | None = None,
        *,
        category: Iterable[str] | None = None,
    ) -> JsonList:
        """``GET /athlete/{id}/events.json?oldest=&newest=`` (inclusive local dates)."""
        params: dict[str, Any] = {"oldest": _iso_date(oldest)}
        if newest is not None:
            params["newest"] = _iso_date(newest)
        if category:
            params["category"] = ",".join(category)
        return _as_list(self.get(f"/athlete/{self.resolve_athlete_id()}/events.json", **params))

    def get_fitness_model_events(self) -> JsonList:
        """``GET /athlete/{id}/fitness-model-events`` (SET_EFTP / SET_FITNESS / FITNESS_DAYS)."""
        return _as_list(self.get(f"/athlete/{self.resolve_athlete_id()}/fitness-model-events"))


# ---------------------------------------------------------------------------- helpers


def _clean_params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _retry_after_seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = dt.datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())


def _iso_local(value: DateLike) -> str:
    """Local ISO-8601 date or date-time without offset (what icu expects)."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None, microsecond=0).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _iso_date(value: DateLike) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def _as_list(data: Any) -> JsonList:
    if data is None:
        return []
    if not isinstance(data, list):
        raise IngestError(f"expected a JSON array, got {type(data).__name__}")
    return [d for d in data if isinstance(d, dict)]


def _as_dict(data: Any) -> JsonDict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise IngestError(f"expected a JSON object, got {type(data).__name__}")
    return data
