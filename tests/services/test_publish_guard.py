"""Write guard and publish gating (ADR-0006 §4, services/publish.py)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from cyp.core.errors import IngestError
from cyp.ingest.intervals.sync import CURSOR_ATHLETE_ID
from cyp.ingest.intervals.sync import SOURCE as ICU_SOURCE
from cyp.planning.job import PlanRun, build_plan
from cyp.services.context import AppContext
from cyp.services.publish import (
    PublishBlockedError,
    PublishFailedError,
    WriteGuardError,
    publish_plan,
    store_upsert_mode,
)
from cyp.store.repo.sync_cursors import SyncCursorRepo
from tests.api.conftest import NOW, TODAY
from tests.publish.conftest import FakeCalendar


class FakeIcu(FakeCalendar):
    def __init__(self, owner: str) -> None:
        super().__init__()
        self.owner = owner
        self.athlete_calls = 0

    def get_athlete(self) -> dict[str, Any]:
        self.athlete_calls += 1
        return {"id": self.owner}

    def close(self) -> None:
        pass


def _setup(ctx: AppContext, synced: str | None, mode: str | None = "upsert") -> None:
    with ctx.factory() as s:
        if synced:
            SyncCursorRepo(s).set(ICU_SOURCE, CURSOR_ATHLETE_ID, synced)
        s.commit()
    if mode:
        store_upsert_mode(ctx, mode)


def _run(ctx: AppContext) -> PlanRun:
    return build_plan(ctx.factory, ctx.athlete_config(), today=TODAY, now_local=NOW, persist=True)


def _ctx_with_key(ctx: AppContext, **extra: Any) -> AppContext:
    ctx.settings = ctx.settings.model_copy(update={"intervals_api_key": _secret("k"), **extra})
    return ctx


def _secret(v: str) -> SecretStr:
    return SecretStr(v)


def test_propose_never_checks_or_writes(ctx: AppContext) -> None:
    _setup(_ctx_with_key(ctx), synced=None, mode=None)
    cal = FakeIcu(owner="i999")
    result = publish_plan(ctx, _run(ctx), None, NOW, write=False, client_factory=lambda k, a: cal)
    assert result.mode == "propose" and cal.writes == [] and cal.athlete_calls == 0


def test_write_to_matching_athlete(ctx: AppContext) -> None:
    _setup(_ctx_with_key(ctx), synced="i42")
    cal = FakeIcu(owner="i42")
    result = publish_plan(ctx, _run(ctx), None, NOW, write=True, client_factory=lambda k, a: cal)
    assert result.written > 0 and cal.athlete_calls == 1


@pytest.mark.parametrize(
    ("synced", "owner", "configured", "match"),
    [
        (None, "i42", "0", "unknown target athlete"),
        ("i42", "i7", "0", "belongs to i7"),
        ("i42", "i42", "i7", "INTERVALS_ATHLETE_ID=i7"),
    ],
)
def test_write_guard_refuses(
    ctx: AppContext, synced: str | None, owner: str, configured: str, match: str
) -> None:
    _setup(_ctx_with_key(ctx, intervals_athlete_id=configured), synced=synced)
    cal = FakeIcu(owner=owner)
    with pytest.raises(WriteGuardError, match=match):
        publish_plan(ctx, _run(ctx), None, NOW, write=True, client_factory=lambda k, a: cal)
    assert cal.writes == []


def test_blocked_without_key_mode_or_feature(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _run(ctx)
    fake = FakeIcu("i42")
    with pytest.raises(PublishBlockedError, match="INTERVALS_API_KEY"):
        publish_plan(ctx, run, None, NOW, write=False, client_factory=lambda k, a: fake)
    _setup(_ctx_with_key(ctx), synced="i42", mode=None)
    with pytest.raises(PublishBlockedError, match="upsert mode"):
        publish_plan(ctx, run, None, NOW, write=True, client_factory=lambda k, a: fake)
    ctx.settings = ctx.settings.model_copy(update={"cyp_features": "publish.calendar=off"})
    with pytest.raises(PublishBlockedError, match=r"publish\.calendar"):
        publish_plan(ctx, run, None, NOW, write=False, client_factory=lambda k, a: fake)
    assert fake.writes == []


class FailingIcu(FakeIcu):
    def bulk_upsert_events(self, events: Any, *, mode: Any = "upsert") -> Any:
        raise IngestError("HTTP 503 from intervals.icu")


def test_failed_write_raises(ctx: AppContext) -> None:
    _setup(_ctx_with_key(ctx), synced="i42")
    cal = FailingIcu(owner="i42")
    with pytest.raises(PublishFailedError, match="503"):
        publish_plan(ctx, _run(ctx), None, NOW, write=True, client_factory=lambda k, a: cal)


def test_a_plan_that_needs_review_is_never_written(ctx: AppContext) -> None:
    """Review 2026-10-08: the invariant lived only in the callers."""
    _setup(_ctx_with_key(ctx), synced="i42")
    run = _run(ctx)
    run.violations = [object()]  # type: ignore[list-item]
    cal = FakeIcu(owner="i42")
    with pytest.raises(PublishBlockedError, match="needs review"):
        publish_plan(ctx, run, None, NOW, write=True, client_factory=lambda k, a: cal)
    assert cal.writes == [] and cal.athlete_calls == 0
