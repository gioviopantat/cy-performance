"""Webhook pure functions: subscription challenge and event parsing."""

from __future__ import annotations

import pytest

from cyp.core.errors import IngestError
from cyp.ingest.strava.webhook import StravaWebhookEvent, parse_event, verify_challenge
from tests.ingest.strava.conftest import load_fixture

TOKEN = "verify-me"


def test_verify_challenge_ok() -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": TOKEN,
        "hub.challenge": "15f7d1a91c1f40f8a748fd134752feb3",
    }
    assert verify_challenge(params, TOKEN) == {"hub.challenge": "15f7d1a91c1f40f8a748fd134752feb3"}
    # parse_qs-style list values
    listed = {k: [v] for k, v in params.items()}
    assert verify_challenge(listed, TOKEN)["hub.challenge"] == params["hub.challenge"]


@pytest.mark.parametrize(
    ("params", "match"),
    [
        ({"hub.mode": "unsubscribe", "hub.verify_token": TOKEN, "hub.challenge": "c"}, "hub.mode"),
        ({"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "c"}, "mismatch"),
        ({"hub.mode": "subscribe", "hub.challenge": "c"}, "mismatch"),
        ({"hub.mode": "subscribe", "hub.verify_token": TOKEN}, "challenge"),
    ],
)
def test_verify_challenge_rejects(params: dict[str, str], match: str) -> None:
    with pytest.raises(IngestError, match=match):
        verify_challenge(params, TOKEN)


def test_verify_challenge_requires_configured_token() -> None:
    with pytest.raises(IngestError, match="mismatch"):
        verify_challenge(
            {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "c"}, ""
        )


def test_parse_event_fixture() -> None:
    event = parse_event(load_fixture("webhook_event.json"))
    assert isinstance(event, StravaWebhookEvent)
    assert event.object_type == "activity" and event.aspect_type == "create"
    assert event.object_id == 1003 and event.owner_id == 188844906
    assert event.subscription_id == 120475 and event.updates == {}
    assert event.is_deauthorization is False


def test_parse_event_update_and_deauth() -> None:
    update = parse_event(
        {
            "object_type": "activity",
            "aspect_type": "update",
            "object_id": "1003",
            "owner_id": 1,
            "updates": {"title": "Renamed", "type": "Ride", "private": "false"},
            "unknown_field": 1,
        }
    )
    assert update.object_id == 1003 and update.updates["title"] == "Renamed"
    deauth = parse_event(
        {
            "object_type": "athlete",
            "aspect_type": "update",
            "object_id": 1,
            "owner_id": 1,
            "updates": {"authorized": "false"},
        }
    )
    assert deauth.is_deauthorization is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"object_type": "segment", "aspect_type": "create", "object_id": 1, "owner_id": 1},
        {"object_type": "activity", "aspect_type": "created", "object_id": 1, "owner_id": 1},
        {"object_type": "activity", "aspect_type": "create", "object_id": "abc", "owner_id": 1},
    ],
)
def test_parse_event_rejects(payload: dict[str, object]) -> None:
    with pytest.raises(IngestError, match="invalid Strava webhook event"):
        parse_event(payload)
