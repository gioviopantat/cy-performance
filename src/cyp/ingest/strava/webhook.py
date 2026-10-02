"""Strava webhook helpers: pure functions, no server (``cyp serve`` wires them in M6).

Subscription validation: Strava sends a ``GET`` to the callback with ``hub.mode=subscribe``,
``hub.verify_token`` and ``hub.challenge``; we must echo ``{"hub.challenge": ...}`` within 2 s.
Events arrive as ``POST`` JSON ``{object_type, aspect_type, object_id, owner_id, subscription_id,
event_time, updates}``.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cyp.core.errors import IngestError


class StravaWebhookEvent(BaseModel):
    """One webhook push (``POST`` body)."""

    model_config = ConfigDict(extra="ignore")

    object_type: Literal["activity", "athlete"]
    aspect_type: Literal["create", "update", "delete"]
    object_id: int
    owner_id: int
    subscription_id: int | None = None
    event_time: int | None = None
    updates: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_deauthorization(self) -> bool:
        """True when the athlete revoked access (we must purge Strava-sourced data)."""
        return (
            self.object_type == "athlete"
            and self.aspect_type == "update"
            and str(self.updates.get("authorized", "")).lower() == "false"
        )


def verify_challenge(params: Mapping[str, Any], verify_token: str) -> dict[str, str]:
    """Validate a subscription handshake and return the body to echo back.

    ``params`` is the query mapping (``hub.mode``, ``hub.verify_token``, ``hub.challenge``);
    list values (as produced by ``parse_qs``) are accepted.

    Raises:
        IngestError: wrong mode, missing challenge, or token mismatch.
    """
    mode = _scalar(params.get("hub.mode"))
    token = _scalar(params.get("hub.verify_token"))
    challenge = _scalar(params.get("hub.challenge"))
    if mode != "subscribe":
        raise IngestError(f"unexpected hub.mode {mode!r}")
    if not challenge:
        raise IngestError("missing hub.challenge")
    if not verify_token or token is None or not hmac.compare_digest(token, verify_token):
        raise IngestError("hub.verify_token mismatch")
    return {"hub.challenge": challenge}


def parse_event(payload: Mapping[str, Any]) -> StravaWebhookEvent:
    """Parse a webhook ``POST`` body.

    Raises:
        IngestError: payload does not match the documented event shape.
    """
    try:
        return StravaWebhookEvent.model_validate(dict(payload))
    except ValidationError as exc:
        raise IngestError(f"invalid Strava webhook event: {exc.error_count()} error(s)") from exc


def _scalar(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        value = value[0] if value else None
    return None if value is None else str(value)
