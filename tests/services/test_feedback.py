"""Post-ride feeling: store, API round trip, readiness uses it (spec personalized-planning §8)."""

from __future__ import annotations

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
