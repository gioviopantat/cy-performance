"""Post-ride feeling: store, API round trip, readiness uses it (spec personalized-planning §8)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cyp.api.app import create_app
from cyp.services.context import AppContext
from cyp.services.ride_feedback import all_answers, get, save
from cyp.store.models import Activity


def _ride(ctx: AppContext) -> int:
    with ctx.factory() as s:
        a = (
            s.query(Activity)
            .filter(Activity.is_ride.is_(True))
            .order_by(Activity.start_local.desc())
            .first()
        )
        assert a is not None
        return a.id


def test_store_and_validation(ctx: AppContext) -> None:
    aid = _ride(ctx)
    assert get(ctx, aid).source in ("none", "intervals.icu")
    assert save(ctx, aid, 8, 4).source == "web"
    assert all_answers(ctx) == {aid: (8, 4)}
    assert save(ctx, aid, None, None).source != "web"
    for bad in ((11, None), (None, 6)):
        try:
            save(ctx, aid, *bad)
        except Exception as exc:  # noqa: BLE001
            assert "must be" in str(exc)
        else:
            raise AssertionError("out of range accepted")


def test_api_round_trip_recomputes_readiness(ctx: AppContext) -> None:
    aid = _ride(ctx)
    with TestClient(create_app(ctx)) as cl:
        r = cl.post(f"/v1/activities/{aid}/feedback", json={"rpe": 9, "feel": 5})
        assert r.status_code == 200 and r.json()["source"] == "web"
        assert cl.get(f"/v1/activities/{aid}/feedback").json()["rpe"] == 9
        assert cl.post(f"/v1/activities/{aid}/feedback", json={"rpe": 0}).status_code == 422


def test_web_answer_wins_per_field(ctx: AppContext) -> None:
    aid = _ride(ctx)
    with ctx.factory() as s:
        a = s.get(Activity, aid)
        assert a is not None
        a.icu_rpe, a.feel = 5, 2
        s.commit()
    got = save(ctx, aid, 7, None)  # RPE answered here, feel only in intervals.icu
    assert (got.rpe, got.feel, got.source) == (7, 2, "web")


# ------------------------------------------------------------------ casual tag (casual-and-strava)


def test_casual_tag_round_trip_keeps_the_answers(ctx: AppContext) -> None:
    from cyp.services.ride_feedback import casual_ids, set_casual

    aid = _ride(ctx)
    assert get(ctx, aid).casual is False and casual_ids(ctx) == frozenset()
    save(ctx, aid, 6, 2)
    got = set_casual(ctx, aid, True)
    assert (got.rpe, got.feel, got.casual) == (6, 2, True)
    assert casual_ids(ctx) == frozenset({aid})
    # A later RPE answer without ``casual`` keeps the tag; readiness still gets the answers.
    assert save(ctx, aid, 7, 2).casual is True
    assert all_answers(ctx) == {aid: (7, 2)}
    assert save(ctx, aid, 7, 2, casual=False).casual is False
    # Tag only (no answers): the file stays; untagging it removes the file.
    save(ctx, aid, None, None)
    assert set_casual(ctx, aid, True).casual is True
    assert set_casual(ctx, aid, False).casual is False
    assert not (ctx.settings.cyp_data_dir / "ride_feedback" / f"{aid}.json").exists()


def test_casual_tag_starts_no_watcher_tick(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyp.services import ride_feedback

    started: list[bool] = []
    monkeypatch.setattr(ride_feedback, "_trigger_watch", lambda _c: started.append(True))
    aid = _ride(ctx)
    ride_feedback.set_casual(ctx, aid, True)
    ride_feedback.save(ctx, aid, None, None, casual=True)
    assert started == []  # tagging never sends; an answered RPE does
    ride_feedback.save(ctx, aid, 5, None)
    assert started == [True]


def test_casual_days_need_every_ride_of_the_day(ctx: AppContext) -> None:
    from cyp.services.ride_feedback import casual_days, set_casual

    ds = ctx.dataset()
    by_day: dict[object, list[int]] = {}
    for a in ds.activities:
        if a.is_ride:
            by_day.setdefault(a.date, []).append(a.id)
    day, ids = next((d, i) for d, i in by_day.items() if len(i) == 1)
    set_casual(ctx, ids[0], True)
    assert casual_days(ctx, ds) == frozenset({day})
    multi = next(((d, i) for d, i in by_day.items() if len(i) > 1), None)
    if multi is not None:
        set_casual(ctx, multi[1][0], True)
        assert multi[0] not in casual_days(ctx, ds)


def test_api_casual_flag(ctx: AppContext) -> None:
    aid = _ride(ctx)
    with TestClient(create_app(ctx)) as cl:
        r = cl.post(
            f"/v1/activities/{aid}/feedback", json={"rpe": None, "feel": None, "casual": True}
        )
        assert r.status_code == 200 and r.json()["casual"] is True
        r = cl.post(f"/v1/activities/{aid}/feedback", json={"rpe": 4, "feel": None})
        assert r.json()["casual"] is True and r.json()["rpe"] == 4
        assert cl.get(f"/v1/activities/{aid}/feedback").json()["casual"] is True
