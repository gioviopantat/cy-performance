"""Publish a plan to the intervals.icu calendar, behind the write guard (ADR-0005, ADR-0006 §4).

Used by ``cyp plan --publish/--apply`` and the autopilot (``cyp run``). Writing requires all of:

1. the caller asks for it (``write=True``: ``--apply --confirm-write``, or the autopilot on a
   profile whose ``planner.mode`` is ``apply``);
2. feature ``publish.calendar`` is on;
3. the upsert mode is known (``cyp publish spike`` or ``cyp profile add`` stored it);
4. the write guard: the API key belongs to the athlete this profile is for.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any, Literal, Protocol

from cyp.core.errors import CypError
from cyp.ingest.intervals.auth import ApiKeyAuth
from cyp.ingest.intervals.client import IntervalsClient
from cyp.ingest.intervals.sync import CURSOR_ATHLETE_ID
from cyp.ingest.intervals.sync import SOURCE as ICU_SOURCE
from cyp.planning.job import PlanRun
from cyp.profiles import open_store
from cyp.publish.plan_events import climbs_from_config, event_specs
from cyp.publish.publisher import CalendarClient, Publisher, PublishResult
from cyp.services.context import AppContext
from cyp.settings import AthleteConfig
from cyp.store.repo.sync_cursors import SyncCursorRepo

CURSOR_SOURCE = "intervals"
CURSOR_UPSERT_MODE = "publish_upsert_mode"


class IcuCalendarClient(CalendarClient, Protocol):
    """What publishing needs from an intervals.icu client (fakes implement this in tests)."""

    def get_athlete(self) -> dict[str, Any]:
        """``GET /athlete/{id}``."""
        ...

    def close(self) -> None:
        """Release the HTTP connection."""
        ...


#: ``(api_key, athlete_id) -> client``; tests inject fakes.
ClientFactory = Callable[[str, str], IcuCalendarClient]


class PublishBlockedError(CypError):
    """Publishing cannot run (no key, feature off, upsert mode unknown)."""


class WriteGuardError(CypError):
    """The API key does not belong to the athlete this profile/database is for."""


class PublishFailedError(CypError):
    """intervals.icu rejected or failed the write (nothing or only part was written)."""


def _default_client(key: str, athlete_id: str) -> IcuCalendarClient:
    return IntervalsClient(auth=ApiKeyAuth(key), athlete_id=athlete_id)


def expected_athlete_id(ctx: AppContext) -> str | None:
    """The icu athlete id this run must write to.

    With a profile: only ``profile.yaml`` ``icu_athlete_id`` (never the sync cursor, which a
    wrong key would overwrite). Legacy layout: the athlete id the DB last synced.
    """
    slug = ctx.settings.cyp_profile
    if slug:
        return open_store(ctx.settings.cyp_profiles_dir).get(slug).meta.icu_athlete_id
    with ctx.factory() as s:
        return SyncCursorRepo(s).get(ICU_SOURCE, CURSOR_ATHLETE_ID)


def key_owner(key: str, *, client_factory: ClientFactory = _default_client) -> str:
    """Icu athlete id the API key belongs to (``GET /athlete/0``)."""
    client = client_factory(key, "0")
    try:
        return str(client.get_athlete().get("id") or "")
    finally:
        client.close()


def check_write_guard(
    ctx: AppContext, key: str, *, client_factory: ClientFactory = _default_client
) -> str:
    """Return the verified athlete id, or raise.

    Raises:
        WriteGuardError: unknown target athlete, or the key / configured id points elsewhere.
    """
    expected = expected_athlete_id(ctx)
    if not expected:
        raise WriteGuardError(
            "unknown target athlete: set icu_athlete_id in profile.yaml (legacy layout: run "
            "`cyp sync` first)"
        )
    configured = ctx.settings.intervals_athlete_id
    if configured not in ("", "0", expected):
        raise WriteGuardError(
            f"INTERVALS_ATHLETE_ID={configured} but this profile is for {expected}; "
            "refusing to write"
        )
    owner = key_owner(key, client_factory=client_factory)
    if owner != expected:
        raise WriteGuardError(
            f"the API key belongs to {owner or 'nobody'} but this profile is for {expected}; "
            "refusing to write to someone else's calendar"
        )
    return expected


def publish_plan(
    ctx: AppContext,
    run: PlanRun,
    cfg: AthleteConfig | None,
    now_local: dt.datetime,
    *,
    write: bool,
    client_factory: ClientFactory = _default_client,
) -> PublishResult:
    """Diff ``run`` against the live calendar; with ``write`` apply it (see module docstring).

    Raises:
        PublishBlockedError: the plan needs review, no API key, ``publish.calendar`` off, or
            upsert mode unknown.
        WriteGuardError: key/athlete mismatch (only checked when writing).
        PublishFailedError: intervals.icu failed the write.
        CypError: API failures from the publisher.
    """
    if write and run.needs_review:  # callers check too; this is the last line (CLAUDE.md)
        raise PublishBlockedError(
            f"the plan needs review ({len(run.violations)} guardrail violation(s)): not writing"
        )
    if write and ctx.athlete_config_or_none() is None:
        raise PublishBlockedError(f"{ctx.athlete_config_path} missing or invalid: not writing")
    features = ctx.features()
    if not features.enabled("publish.calendar"):
        raise PublishBlockedError("feature publish.calendar is off")
    key = ctx.settings.intervals_api_key.get_secret_value()
    if not key:
        raise PublishBlockedError("INTERVALS_API_KEY is not set: cannot reach the calendar")
    with ctx.factory() as s:
        mode = SyncCursorRepo(s).get(CURSOR_SOURCE, CURSOR_UPSERT_MODE)
    if write and mode not in ("upsert", "uid"):
        raise PublishBlockedError(
            "upsert mode unknown: run `cyp publish spike --confirm-write` once for this profile"
        )
    athlete_id = ctx.settings.intervals_athlete_id or "0"
    if write:
        athlete_id = check_write_guard(ctx, key, client_factory=client_factory)
    climbs = climbs_from_config(cfg) if features.enabled("plan.climb_routes") else []
    specs = event_specs(run, climbs=climbs)
    window = (run.days[0].date, run.days[-1].date) if run.days else (run.today, run.today)
    client = client_factory(key, athlete_id)
    try:
        upsert_mode: Literal["upsert", "uid"] = "uid" if mode == "uid" else "upsert"
        result = Publisher(client, ctx.factory, upsert_mode=upsert_mode).run(
            specs,
            window=window,
            now_local=now_local,
            mode="apply" if write else "propose",
            allow_write=write,
        )
    finally:
        client.close()
    if result.failed:
        raise PublishFailedError(f"intervals.icu write failed: {result.failed}")
    return result


def store_upsert_mode(ctx: AppContext, mode: str) -> None:
    """Remember the upsert mode the publisher should use for this profile's calendar."""
    with ctx.factory() as s:
        SyncCursorRepo(s).set(CURSOR_SOURCE, CURSOR_UPSERT_MODE, mode)
        s.commit()
