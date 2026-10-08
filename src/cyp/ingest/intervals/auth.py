"""Auth strategies for the intervals.icu client (docs/02 §3.1).

v1 uses an API key over HTTP Basic (username literally ``API_KEY``). The strategy is a small
protocol so an OAuth bearer-token strategy can be dropped in later without touching the client.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable


@runtime_checkable
class AuthStrategy(Protocol):
    """Anything that can decorate outgoing request headers with credentials."""

    def headers(self) -> dict[str, str]:
        """Headers to attach to every request (e.g. ``Authorization``)."""
        ...

    def describe(self) -> str:
        """Short, secret-free description for logs and ``cyp doctor``."""
        ...


class ApiKeyAuth:
    """HTTP Basic with username ``API_KEY`` and the developer key as password."""

    USERNAME = "API_KEY"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("intervals.icu API key is empty")
        self._api_key = api_key

    def headers(self) -> dict[str, str]:
        """``Authorization: Basic base64(API_KEY:<key>)``."""
        token = base64.b64encode(f"{self.USERNAME}:{self._api_key}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def describe(self) -> str:
        """Masked description."""
        return "api_key(***)"


class BearerAuth:
    """OAuth2 bearer token (multi-user apps; not used in v1, kept for the protocol's sake)."""

    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("access token is empty")
        self._token = access_token

    def headers(self) -> dict[str, str]:
        """``Authorization: Bearer <token>``."""
        return {"Authorization": f"Bearer {self._token}"}

    def describe(self) -> str:
        """Masked description."""
        return "oauth_bearer(***)"
