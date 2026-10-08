"""Write a ride's RIDE.LOG to its Strava description (flag ``strava.write_description``).

The only Strava write in the project. Gates, in order:

1. feature ``strava.write_description`` is on for the profile (default off);
2. the ride has a Strava id;
3. the stored token carries the ``activity:write`` scope (``cyp auth strava`` after adding it to
   ``STRAVA_SCOPE``);
4. write guard: the token belongs to the athlete who owns the ride (Strava athlete id of the
   ride as fetched from Strava; fails closed when either id is unknown);
5. the caller confirmed (CLI ``--confirm-write``, API ``confirm=true``).

The athlete's own text is kept: a previous ``📋 WORKOUT`` / ``⚙️ RIDE.LOG`` block (markers at the
start of a line) is replaced and everything before and after it stays; without one, the
RIDE.LOG is appended after a blank line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cyp.core.errors import CypError
from cyp.services.context import AppContext
from cyp.store.models import Activity

MARKERS = ("📋 WORKOUT", "⚙️ RIDE.LOG")
WRITE_SCOPE = "activity:write"


class StravaWriteBlockedError(CypError):
    """A gate refused the write (flag, scope, owner, missing Strava id)."""


@dataclass(frozen=True)
class PushResult:
    """What was (or would be) written."""

    strava_id: int
    before: str
    after: str
    written: bool


def _is_marker(line: str, name: str) -> bool:
    """``line`` starts our block section ``name`` (emoji with or without U+FE0F)."""
    return line.replace("\ufe0f", "").lstrip().startswith(name.replace("\ufe0f", ""))


def _block_span(lines: list[str]) -> tuple[int, int] | None:
    """``[start, end)`` of our block.

    A WORKOUT and/or RIDE.LOG paragraph starting at a line start, plus the RIDE.LOG
    paragraph's quoted poem (``> ``) even after a blank line.
    """
    start = next((i for i, ln in enumerate(lines) if any(_is_marker(ln, m) for m in MARKERS)), None)
    if start is None:
        return None
    i = start
    while True:
        while i < len(lines) and lines[i].strip():
            i += 1
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and (
            _is_marker(lines[j], MARKERS[1]) or lines[j].lstrip().startswith(">")
        ):
            i = j
            continue
        return start, i


def compose(existing: str | None, ride_log: str, previous: str | None = None) -> str:
    """The new description: the athlete's own text kept, only our block replaced or appended.

    ``previous``: the text we last wrote; replaced exactly when found. Otherwise our block is
    found by its markers at the start of a line (not mid-sentence) and replaced, keeping the
    athlete's text before and after it; with no block the RIDE.LOG is appended.
    """
    existing = (existing or "").rstrip()
    if not existing:
        return ride_log
    if previous and previous.strip() and previous.strip() in existing:
        return existing.replace(previous.strip(), ride_log, 1)
    lines = existing.split("\n")
    span = _block_span(lines)
    if span is None:
        return f"{existing}\n\n{ride_log}"
    head = "\n".join(lines[: span[0]]).rstrip()
    tail = "\n".join(lines[span[1] :]).strip()
    return "\n\n".join(part for part in (head, ride_log, tail) if part)


#: ``() -> client`` with ``get_activity`` / ``update_activity_description``; tests inject fakes.
ClientFactory = Callable[[], Any]


def _default_client(ctx: AppContext) -> ClientFactory:
    def build() -> Any:
        from cyp.ingest.strava.client import StravaClient
        from cyp.ingest.strava.oauth import StravaAuth, TokenStore

        auth = StravaAuth(ctx.settings, TokenStore.from_settings(ctx.settings))
        return StravaClient(
            auth.get_valid_access_token, auth.force_refresh_access_token, allow_write=True
        )

    return build


def _token_gate(ctx: AppContext, activity: Activity) -> int:
    from cyp.ingest.strava.oauth import TokenStore

    token = TokenStore.from_settings(ctx.settings).load()
    if token is None:
        raise StravaWriteBlockedError("no Strava token: run `cyp auth strava`")
    scopes = (token.scope or "").replace(",", " ").split()
    if WRITE_SCOPE not in scopes:
        raise StravaWriteBlockedError(
            f"the Strava token lacks {WRITE_SCOPE}: add it to STRAVA_SCOPE in the profile's .env "
            "and run `cyp auth strava` again"
        )
    if token.athlete_id is None:
        raise StravaWriteBlockedError(
            "the Strava token has no athlete id: run `cyp auth strava` again; refusing to write"
        )
    stored = ((activity.raw_strava_json or {}).get("athlete") or {}).get("id")
    if stored is not None:  # cheap early refusal; the fetched ride is checked again in push()
        _owner_gate(token.athlete_id, stored)
    return token.athlete_id


def _owner_gate(token_athlete: int, owner: object) -> None:
    """Fails closed: an unknown ride owner is a refusal, not a pass."""
    if owner is None:
        raise StravaWriteBlockedError("cannot tell who owns this Strava ride; refusing to write")
    if int(str(owner)) != token_athlete:
        raise StravaWriteBlockedError(
            f"the Strava token belongs to athlete {token_athlete}, the ride to {owner}; "
            "refusing to write"
        )


def push(
    ctx: AppContext,
    activity_id: int,
    text: str,
    *,
    write: bool,
    client_factory: ClientFactory | None = None,
) -> PushResult:
    """Preview (``write=False``) or write ``text`` as the ride's RIDE.LOG on Strava.

    Raises:
        StravaWriteBlockedError: a gate refused (see the module docstring).
    """
    if not ctx.features().enabled("strava.write_description"):
        raise StravaWriteBlockedError(
            "feature strava.write_description is off (set it under features: in athlete.yaml)"
        )
    if not text.strip():
        raise StravaWriteBlockedError("empty RIDE.LOG text")
    with ctx.factory() as s:
        activity = s.get(Activity, activity_id)
        if activity is None or not activity.strava_id:
            raise StravaWriteBlockedError(f"activity {activity_id} has no Strava id")
        token_athlete = _token_gate(ctx, activity)
        strava_id = int(activity.strava_id)
    client = (client_factory or _default_client(ctx))()
    remote = client.get_activity(strava_id, include_all_efforts=False)
    _owner_gate(token_athlete, (remote.get("athlete") or {}).get("id"))
    before = str(remote.get("description") or "")
    from cyp.services.ride_log import load_saved

    after = compose(before, text.strip(), previous=load_saved(ctx, activity_id))
    if not write or after == before.rstrip():
        return PushResult(strava_id, before, after, written=False)
    client.update_activity_description(strava_id, after)
    from cyp.services.ride_log import save

    save(ctx, activity_id, text)  # what went to Strava is what the UI shows from now on
    return PushResult(strava_id, before, after, written=True)
