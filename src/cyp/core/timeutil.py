"""Time helpers. Storage convention: ISO-8601 UTC in ``*_utc``, athlete-local in ``*_local``."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Taipei"
UTC = dt.UTC


def tz(name: str = DEFAULT_TZ) -> ZoneInfo:
    """Return the ``ZoneInfo`` for an IANA name."""
    return ZoneInfo(name)


def now_utc() -> dt.datetime:
    """Current aware UTC datetime (microseconds stripped for stable storage)."""
    return dt.datetime.now(UTC).replace(microsecond=0)


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Coerce a datetime to aware UTC; naive input is assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_local(value: dt.datetime, tz_name: str = DEFAULT_TZ) -> dt.datetime:
    """Convert to the athlete's local zone."""
    return ensure_utc(value).astimezone(tz(tz_name))


def iso_utc(value: dt.datetime) -> str:
    """Serialise as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return ensure_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_local(value: dt.datetime, tz_name: str = DEFAULT_TZ) -> str:
    """Serialise in local time with offset, e.g. ``2026-10-05T06:30:00+08:00``."""
    return to_local(value, tz_name).isoformat(timespec="seconds")


def parse_iso(value: str) -> dt.datetime:
    """Parse ISO-8601 (accepts trailing ``Z``) into an aware UTC datetime."""
    text = value.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    return ensure_utc(parsed)


def local_date(value: dt.datetime, tz_name: str = DEFAULT_TZ) -> dt.date:
    """Calendar date of an instant in the athlete's zone."""
    return to_local(value, tz_name).date()


def today_local(tz_name: str = DEFAULT_TZ) -> dt.date:
    """Today's date in the athlete's zone."""
    return dt.datetime.now(tz(tz_name)).date()


def week_start(day: dt.date) -> dt.date:
    """Monday of the ISO week containing ``day``."""
    return day - dt.timedelta(days=day.weekday())


def epoch_s(value: dt.datetime) -> int:
    """Unix epoch seconds (used for Strava ``after=`` cursors)."""
    return int(ensure_utc(value).timestamp())
