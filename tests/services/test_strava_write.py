"""RIDE.LOG -> Strava description: composition and every gate (services/strava_write.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from cyp.ingest.strava.oauth import StravaToken, TokenStore
from cyp.services.context import AppContext
from cyp.services.strava_write import StravaWriteBlockedError, compose, push
from cyp.store.models import Activity


def test_compose_keeps_the_athletes_text() -> None:
    log = "📋 WORKOUT\nnew\n\n⚙️ RIDE.LOG\nX : 1"
    assert compose("", log) == log
    assert compose("好累的一天", log) == f"好累的一天\n\n{log}"
    old = "好累的一天\n\n📋 WORKOUT\nold\n\n⚙️ RIDE.LOG\nX : 0\n> 舊詩"
    assert compose(old, log) == f"好累的一天\n\n{log}"
    assert compose("⚙️ RIDE.LOG\nonly", log) == log


def test_compose_keeps_text_after_our_block() -> None:
    """Review 2026-10-08: text after the block was dropped."""
    log = "📋 WORKOUT\nnew\n\n⚙️ RIDE.LOG\nX : 1"
    old = (
        "Great ride\n\n📋 WORKOUT\nold\n\n⚙️ RIDE.LOG\nX : 0\n\n> 舊詩\n> 第二行"
        "\n\nPS: flat tyre at km 40"
    )
    assert compose(old, log) == f"Great ride\n\n{log}\n\nPS: flat tyre at km 40"


def test_compose_ignores_markers_mid_sentence() -> None:
    log = "⚙️ RIDE.LOG\nX : 1"
    assert compose("Felt 📋 WORKOUT was hard", log) == f"Felt 📋 WORKOUT was hard\n\n{log}"


def test_compose_matches_the_gear_without_variation_selector() -> None:
    log = "⚙️ RIDE.LOG\nX : 1"
    assert compose("心得\n\n⚙ RIDE.LOG\nX : 0", log) == f"心得\n\n{log}"


def test_compose_replaces_exactly_what_we_wrote_last() -> None:
    previous = "📋 WORKOUT\n舊\n\n⚙️ RIDE.LOG\nX : 0"
    old = f"前言\n\n{previous}\n後記"
    assert compose(old, "NEW", previous=previous) == "前言\n\nNEW\n後記"


class FakeStrava:
    def __init__(self, description: str = "", owner: int | None = 42) -> None:
        self.description = description
        self.owner = owner
        self.writes: list[tuple[int, str]] = []

    def get_activity(self, activity_id: int, *, include_all_efforts: bool = True) -> dict[str, Any]:
        athlete = {"id": self.owner} if self.owner is not None else {}
        return {"id": activity_id, "description": self.description, "athlete": athlete}

    def update_activity_description(self, activity_id: int, description: str) -> dict[str, Any]:
        self.writes.append((activity_id, description))
        self.description = description
        return {}


def _ride(ctx: AppContext, owner: int = 42) -> int:
    with ctx.factory() as s:
        a = s.query(Activity).filter(Activity.is_ride.is_(True)).first()
        assert a is not None
        a.strava_id = 777
        a.raw_strava_json = {"athlete": {"id": owner}}
        s.commit()
        return a.id


def _token(ctx: AppContext, scope: str, athlete: int = 42) -> None:
    ctx.settings.tokens_dir.mkdir(parents=True, exist_ok=True)
    TokenStore.from_settings(ctx.settings).save(
        StravaToken(
            access_token=SecretStr("a"),
            refresh_token=SecretStr("r"),
            expires_at=4_000_000_000,
            scope=scope,
            athlete_id=athlete,
        )
    )


def _flag(ctx: AppContext, on: bool) -> None:
    value = "on" if on else "off"
    ctx.settings = ctx.settings.model_copy(
        update={"cyp_features": f"strava.write_description={value},sync.strava=on"}
    )


def test_gates_in_order(ctx: AppContext) -> None:
    aid = _ride(ctx)
    fake = FakeStrava("我的心得")
    with pytest.raises(StravaWriteBlockedError, match=r"strava\.write_description"):
        push(ctx, aid, "x", write=True, client_factory=lambda: fake)
    _flag(ctx, True)
    with pytest.raises(StravaWriteBlockedError, match="no Strava token"):
        push(ctx, aid, "x", write=True, client_factory=lambda: fake)
    _token(ctx, "read,activity:read_all")
    with pytest.raises(StravaWriteBlockedError, match="activity:write"):
        push(ctx, aid, "x", write=True, client_factory=lambda: fake)
    _token(ctx, "read,activity:read_all,activity:write", athlete=7)
    with pytest.raises(StravaWriteBlockedError, match="belongs to athlete 7"):
        push(ctx, aid, "x", write=True, client_factory=lambda: fake)
    assert fake.writes == []


def test_owner_check_fails_closed(ctx: AppContext) -> None:
    """Review 2026-10-08: a ride without stored Strava JSON skipped the owner check."""
    aid = _ride(ctx)
    with ctx.factory() as s:
        a = s.get(Activity, aid)
        assert a is not None
        a.raw_strava_json = None
        s.commit()
    _flag(ctx, True)
    _token(ctx, "activity:write")
    for fake, match in (
        (FakeStrava(owner=7), "belongs to athlete 42"),
        (FakeStrava(owner=None), "who owns"),
    ):
        with pytest.raises(StravaWriteBlockedError, match=match):
            push(ctx, aid, "x", write=True, client_factory=lambda f=fake: f)
        assert fake.writes == []


def test_preview_then_write_then_idempotent(ctx: AppContext) -> None:
    aid = _ride(ctx)
    _flag(ctx, True)
    _token(ctx, "read activity:write")
    fake = FakeStrava("我的心得")
    log = "📋 WORKOUT\n完成\n\n⚙️ RIDE.LOG\nSTATUS : FRESH"
    preview = push(ctx, aid, log, write=False, client_factory=lambda: fake)
    assert not preview.written and fake.writes == [] and preview.after.startswith("我的心得\n\n📋")
    done = push(ctx, aid, log, write=True, client_factory=lambda: fake)
    assert done.written and fake.writes == [(777, preview.after)]
    again = push(ctx, aid, log, write=True, client_factory=lambda: fake)
    assert not again.written and len(fake.writes) == 1  # unchanged text: no second write


def test_api_refuses_when_flag_off(ctx: AppContext, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from cyp.api.app import create_app

    aid = _ride(ctx)
    with TestClient(create_app(ctx)) as cl:
        r = cl.post(f"/v1/activities/{aid}/strava-description", json={"text": "x", "confirm": True})
    assert r.status_code == 409 and "strava.write_description" in json.dumps(r.json())


def test_saved_ride_log_wins_and_resets(ctx: AppContext) -> None:
    from cyp.services.ride_log import build, save

    aid = _ride(ctx)
    generated = build(ctx, aid)
    assert generated.saved is None and generated.text == generated.generated
    save(ctx, aid, "📋 WORKOUT\n自己改過\n> 藏頭詩\n")
    assert build(ctx, aid).text == "📋 WORKOUT\n自己改過\n> 藏頭詩"
    save(ctx, aid, "   ")
    assert build(ctx, aid).saved is None


def test_push_saves_what_it_wrote(ctx: AppContext) -> None:
    from cyp.services.ride_log import build

    aid = _ride(ctx)
    _flag(ctx, True)
    _token(ctx, "activity:write")
    push(ctx, aid, "📋 WORKOUT\n含詩", write=True, client_factory=lambda: FakeStrava())
    assert build(ctx, aid).saved == "📋 WORKOUT\n含詩"


def test_api_save_and_restore(ctx: AppContext) -> None:
    from fastapi.testclient import TestClient

    from cyp.api.app import create_app

    aid = _ride(ctx)
    with TestClient(create_app(ctx)) as cl:
        saved = cl.post(f"/v1/activities/{aid}/ride-log", json={"text": "📋 WORKOUT\n> 詩"}).json()
        assert saved["source"] == "saved" and saved["text"].endswith("> 詩")
        reset = cl.post(f"/v1/activities/{aid}/ride-log", json={"text": ""}).json()
        assert reset["source"] == "generated" and reset["text"] == reset["generated"]
        assert cl.post("/v1/activities/999999/ride-log", json={"text": "x"}).status_code == 404


def test_saved_ride_log_is_never_served_from_a_stale_cache(ctx: AppContext) -> None:
    """Regression 2026-10-07: the ETag ignored saved files, so the UI kept the old text (304)."""
    from fastapi.testclient import TestClient

    from cyp.api.app import create_app

    aid = _ride(ctx)
    with TestClient(create_app(ctx)) as cl:
        first = cl.get(f"/v1/activities/{aid}/ride-log")
        assert "etag" not in {k.lower() for k in first.headers}
        cl.post(f"/v1/activities/{aid}/ride-log", json={"text": "📋 WORKOUT\n> 新詩"})
        again = cl.get(f"/v1/activities/{aid}/ride-log", headers={"If-None-Match": '"x"'})
        assert again.status_code == 200 and again.json()["text"].endswith("> 新詩")


def test_overwriting_a_saved_ride_log_keeps_the_previous_one(ctx: AppContext) -> None:
    """Regression 2026-10-07: a stale UI write replaced a hand-written poem with no way back."""
    from cyp.services.ride_log import save, saved_path

    aid = _ride(ctx)
    save(ctx, aid, "📋 WORKOUT\n> 好詩")
    save(ctx, aid, "📋 WORKOUT\n自動版")
    prev = saved_path(ctx, aid).with_suffix(".prev.txt")
    assert prev.read_text(encoding="utf-8").strip() == "📋 WORKOUT\n> 好詩"
    save(ctx, aid, "")
    assert prev.read_text(encoding="utf-8").strip() == "📋 WORKOUT\n自動版"
