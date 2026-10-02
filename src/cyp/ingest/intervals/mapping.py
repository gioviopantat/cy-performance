"""Pure functions: intervals.icu JSON payloads -> row dicts for our tables.

Field names were checked against the live OpenAPI document (docs/02 §3.2). Notable
differences from the prose in docs/02 and 03, resolved here:

- there is no ``icu_eftp`` on ``Activity``; the per-ride eFTP is ``icu_pm_ftp`` (we map it to
  our ``icu_eftp`` column);
- ``icu_zone_times`` is a list of ``{id, secs}`` objects, flattened to a list of seconds;
- ``strava_id`` is a string;
- ``source == "STRAVA"`` is the authoritative Strava-origin marker (``strava_id`` is a fallback).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cyp.core.activity import RIDE_SPORT_TYPES
from cyp.core.athlete import Zone, ZoneModel
from cyp.core.timeutil import DEFAULT_TZ, ensure_utc, iso_utc, parse_iso

JsonDict = dict[str, Any]


# --------------------------------------------------------------------------- small helpers


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    f = _num(value)
    return None if f is None else round(f)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def parse_local_datetime(value: str | None) -> dt.datetime | None:
    """Parse icu's naive local ISO (``2026-09-28T06:12:34``) or a date."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def activity_start(raw: JsonDict, default_tz: str = DEFAULT_TZ) -> tuple[str, str | None, str]:
    """``(start_utc_iso, start_local_iso, tz)`` from ``start_date`` / ``start_date_local``."""
    tz_name = _str(raw.get("timezone")) or default_tz
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name, zone = default_tz, ZoneInfo(default_tz)
    local = parse_local_datetime(_str(raw.get("start_date_local")))
    utc: dt.datetime | None = None
    start_date = _str(raw.get("start_date"))
    if start_date:
        try:
            utc = parse_iso(start_date)
        except ValueError:
            utc = None
    if utc is None and local is not None:
        utc = ensure_utc(local.replace(tzinfo=zone) if local.tzinfo is None else local)
    if utc is None:
        raise ValueError(f"activity {raw.get('id')!r} has no parsable start date")
    if local is None:
        local = utc.astimezone(zone)
    elif local.tzinfo is None:
        local = local.replace(tzinfo=zone)
    return iso_utc(utc), local.isoformat(timespec="seconds"), tz_name


def is_strava_origin(raw: JsonDict) -> bool:
    """True when icu got this activity from Strava (no streams/intervals served)."""
    source = _str(raw.get("source"))
    if source is not None:
        return source.upper() == "STRAVA"
    return bool(_str(raw.get("strava_id")))


def is_stub(raw: JsonDict) -> bool:
    """True for the placeholder icu returns for Strava-origin activities.

    Since Nov 2024 the icu API answers ``{"id": "<stravaId>", "source": "STRAVA",
    "start_date_local": ..., "_note": ...}`` for those: no ``type``, no metrics, no streams.
    The icu ``id`` of such an activity **is** the Strava activity id.
    """
    return is_strava_origin(raw) and raw.get("type") is None


def stub_strava_id(raw: JsonDict) -> int | None:
    """``strava_id`` for an icu activity: the explicit field, else the numeric id of a stub."""
    explicit = _str(raw.get("strava_id"))
    if explicit and explicit.isdigit():
        return int(explicit)
    icu_id = _str(raw.get("id"))
    if is_stub(raw) and icu_id and icu_id.isdigit():
        return int(icu_id)
    return None


#: Columns a stub may safely overwrite on a row that Strava already filled.
STUB_ROW_KEYS: frozenset[str] = frozenset({"intervals_id", "strava_id", "raw_intervals_json"})


def has_streams(raw: JsonDict) -> bool:
    """Whether icu advertises any stream for the activity (``stream_types``)."""
    types = raw.get("stream_types")
    if isinstance(types, list) and types:
        return True
    # Older payloads may omit stream_types; fall back to the recorded-data hints.
    return not is_strava_origin(raw) and (
        bool(raw.get("device_watts")) or bool(raw.get("has_heartrate"))
    )


def zone_times_secs(value: Any) -> list[float] | None:
    """Flatten ``[{id, secs}]`` (or a plain list) to a list of seconds."""
    if not isinstance(value, list):
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, dict):
            out.append(float(item.get("secs") or 0))
        else:
            n = _num(item)
            out.append(n if n is not None else 0.0)
    return out


# ------------------------------------------------------------------------------- activities


def activity_row(
    raw: JsonDict, *, athlete_id: int | None, default_tz: str = DEFAULT_TZ
) -> JsonDict:
    """Map an icu ``Activity`` to ``activities`` columns (docs/03) + ``raw_intervals_json``."""
    start_utc, start_local, tz_name = activity_start(raw, default_tz)
    sport_type = _str(raw.get("type")) or "Other"
    moving = _int(raw.get("moving_time"))
    elapsed = _int(raw.get("elapsed_time"))
    gear = raw.get("gear") if isinstance(raw.get("gear"), dict) else {}
    joules = _num(raw.get("icu_joules"))
    strava_id = stub_strava_id(raw)
    row: JsonDict = {
        "athlete_id": athlete_id,
        "intervals_id": str(raw["id"]),
        "strava_id": strava_id,
        "sport_type": sport_type,
        "is_ride": sport_type in RIDE_SPORT_TYPES,
        "name": _str(raw.get("name")),
        "description": _str(raw.get("description")),
        "start_utc": start_utc,
        "start_local": start_local,
        "tz": tz_name,
        "moving_s": moving if moving is not None else elapsed,
        "elapsed_s": elapsed if elapsed is not None else moving,
        "distance_m": _num(raw.get("icu_distance") or raw.get("distance")),
        "elev_gain_m": _num(raw.get("total_elevation_gain")),
        "trainer": bool(raw.get("trainer")),
        "commute": bool(raw.get("commute")) or raw.get("sub_type") == "COMMUTE",
        "race": bool(raw.get("race")) or raw.get("sub_type") == "RACE",
        "manual": _str(raw.get("source")) == "MANUAL",
        "has_power": bool(raw.get("device_watts")),
        "has_hr": bool(raw.get("has_heartrate")),
        "has_cadence": raw.get("average_cadence") is not None
        or "cadence" in (raw.get("stream_types") or []),
        "device_name": _str(raw.get("device_name")),
        "gear_id": _str(gear.get("id")) if gear else None,
        "gear_name": _str(gear.get("name")) if gear else None,
        "avg_w": _num(raw.get("icu_average_watts")),
        "np_w": _num(raw.get("icu_weighted_avg_watts")),
        "max_w": _num(raw.get("max_watts")),
        "kj": round(joules / 1000.0, 1) if joules is not None else None,
        "avg_hr": _num(raw.get("average_heartrate")),
        "max_hr": _num(raw.get("max_heartrate")),
        "avg_cad": _num(raw.get("average_cadence")),
        "icu_training_load": _num(raw.get("icu_training_load")),
        "icu_intensity": _num(raw.get("icu_intensity")),
        "icu_ftp": _num(raw.get("icu_ftp")),
        "icu_eftp": _num(raw.get("icu_pm_ftp")),
        "icu_pm_cp": _num(raw.get("icu_pm_cp")),
        "icu_pm_w_prime": _num(raw.get("icu_pm_w_prime")),
        "icu_pm_p_max": _num(raw.get("icu_pm_p_max")),
        "icu_decoupling": _num(raw.get("decoupling")),
        "icu_polarization_index": _num(raw.get("polarization_index")),
        "icu_joules_above_ftp": _num(raw.get("icu_joules_above_ftp")),
        "icu_max_wbal_depletion": _num(raw.get("icu_max_wbal_depletion")),
        "icu_zone_times": zone_times_secs(raw.get("icu_zone_times")),
        "icu_hr_zone_times": zone_times_secs(raw.get("icu_hr_zone_times")),
        "paired_event_id": _int(raw.get("paired_event_id")),
        "icu_rpe": _int(raw.get("icu_rpe")),
        "feel": _int(raw.get("feel")),
        "raw_intervals_json": raw,
    }
    if strava_id is not None:
        row["match_method"] = "strava_id"
    return row


INTERVAL_TYPES: frozenset[str] = frozenset({"WORK", "RECOVERY", "WARMUP", "COOLDOWN", "OTHER"})


def interval_rows(payload: JsonDict) -> list[JsonDict]:
    """Map ``GET /activity/{id}/intervals`` -> ``activity_intervals`` rows (``source='icu'``)."""
    items = payload.get("icu_intervals")
    if not isinstance(items, list):
        return []
    rows: list[JsonDict] = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        start = _int(it.get("start_time")) or 0
        end = _int(it.get("end_time"))
        duration = _int(it.get("elapsed_time"))
        if duration is None and end is not None:
            duration = max(0, end - start)
        if duration is None:
            duration = _int(it.get("moving_time")) or 0
        kind = str(it.get("type") or "WORK").upper()
        rows.append(
            {
                "idx": idx,
                "label": _str(it.get("label")),
                "type": kind if kind in INTERVAL_TYPES else "OTHER",
                "start_s": start,
                "duration_s": duration,
                "avg_w": _num(it.get("average_watts")),
                "np_w": _num(it.get("weighted_average_watts")),
                "intensity": _num(it.get("intensity")),
                "avg_hr": _num(it.get("average_heartrate")),
                "max_hr": _num(it.get("max_heartrate")),
                "avg_cad": _num(it.get("average_cadence")),
                "decoupling": _num(it.get("decoupling")),
                "wbal_start": _num(it.get("wbal_start")),
                "wbal_end": _num(it.get("wbal_end")),
                "zone": _int(it.get("zone")),
                "training_load": _num(it.get("training_load")),
                "raw_json": it,
            }
        )
    return rows


# ---------------------------------------------------------------------- athlete + settings


def athlete_row(raw: JsonDict) -> JsonDict:
    """``athletes`` columns from ``GET /athlete/{id}``."""
    name = _str(raw.get("name")) or " ".join(
        p for p in (_str(raw.get("firstname")), _str(raw.get("lastname"))) if p
    )
    strava_id = raw.get("strava_id")
    return {
        "strava_id": int(strava_id) if isinstance(strava_id, int) else None,
        "name": name or None,
        "sex": _str(raw.get("sex")),
        "timezone": _str(raw.get("timezone")) or DEFAULT_TZ,
        "weight_kg": _num(raw.get("icu_weight")) or _num(raw.get("weight")),
        "raw_intervals_json": raw,
    }


def pick_sport_settings(settings: list[JsonDict], sport: str = "Ride") -> JsonDict | None:
    """The ``SportSettings`` entry whose ``types`` includes ``sport`` (else the first entry)."""
    for s in settings:
        types = s.get("types") or []
        if sport in types:
            return s
    return settings[0] if settings else None


def power_zone_model(ftp: float | None, pct_bounds: Any, names: Any) -> dict[str, Any] | None:
    """Icu power zones (upper bounds in % FTP) -> ``ZoneModel`` dump with absolute watts."""
    if not ftp or not isinstance(pct_bounds, list) or not pct_bounds:
        return None
    zones: list[Zone] = []
    lo = 0.0
    labels = names if isinstance(names, list) else []
    for i, pct in enumerate(pct_bounds):
        p = _num(pct)
        if p is None:
            continue
        hi: float | None = None if p >= 900 else round(ftp * p / 100.0, 1)
        zones.append(
            Zone(idx=i + 1, name=str(labels[i]) if i < len(labels) else f"Z{i + 1}", lo=lo, hi=hi)
        )
        lo = hi if hi is not None else lo
    if not zones:
        return None
    return ZoneModel(kind="power", anchor=float(ftp), zones=zones).model_dump()


def hr_zone_model(lthr: float | None, bpm_bounds: Any, names: Any) -> dict[str, Any] | None:
    """Icu HR zones (absolute bpm upper bounds) -> ``ZoneModel`` dump anchored on LTHR."""
    if not lthr or not isinstance(bpm_bounds, list) or not bpm_bounds:
        return None
    zones: list[Zone] = []
    lo = 0.0
    labels = names if isinstance(names, list) else []
    for i, bpm in enumerate(bpm_bounds):
        b = _num(bpm)
        if b is None:
            continue
        hi: float | None = None if b >= 900 else float(b)
        zones.append(
            Zone(idx=i + 1, name=str(labels[i]) if i < len(labels) else f"Z{i + 1}", lo=lo, hi=hi)
        )
        lo = hi if hi is not None else lo
    if not zones:
        return None
    return ZoneModel(kind="hr", anchor=float(lthr), zones=zones).model_dump()


def settings_history_values(sport: JsonDict, athlete: JsonDict) -> JsonDict:
    """``athlete_settings_history`` comparable fields from a ``SportSettings`` + athlete."""
    ftp = _num(sport.get("ftp"))
    lthr = _num(sport.get("lthr"))
    model = sport.get("mmp_model") if isinstance(sport.get("mmp_model"), dict) else {}
    return {
        "ftp": ftp,
        "indoor_ftp": _num(sport.get("indoor_ftp")),
        "eftp": _num(model.get("ftp")) if model else None,
        "w_prime": _num(sport.get("w_prime")),
        "p_max": _num(sport.get("p_max")),
        "lthr": _int(lthr),
        "max_hr": _int(sport.get("max_hr")),
        "resting_hr": _int(athlete.get("icu_resting_hr")),
        "weight_kg": _num(athlete.get("icu_weight")) or _num(athlete.get("weight")),
        "power_zones": power_zone_model(
            ftp, sport.get("power_zones"), sport.get("power_zone_names")
        ),
        "hr_zones": hr_zone_model(lthr, sport.get("hr_zones"), sport.get("hr_zone_names")),
    }


# ------------------------------------------------------------------------------- wellness


def wellness_row(raw: JsonDict) -> tuple[dt.date, JsonDict]:
    """``(date_local, wellness_daily columns)`` from an icu ``Wellness`` object (``id`` = date)."""
    date_local = dt.date.fromisoformat(str(raw["id"])[:10])
    values: JsonDict = {
        "ctl": _num(raw.get("ctl")),
        "atl": _num(raw.get("atl")),
        "ramp_rate": _num(raw.get("rampRate")),
        "ctl_load": _num(raw.get("ctlLoad")),
        "atl_load": _num(raw.get("atlLoad")),
        "resting_hr": _num(raw.get("restingHR")),
        "hrv": _num(raw.get("hrv")),
        "hrv_sdnn": _num(raw.get("hrvSDNN")),
        "sleep_s": _int(raw.get("sleepSecs")),
        "sleep_score": _num(raw.get("sleepScore")),
        "sleep_quality": _int(raw.get("sleepQuality")),
        "avg_sleeping_hr": _num(raw.get("avgSleepingHR")),
        "soreness": _int(raw.get("soreness")),
        "fatigue": _int(raw.get("fatigue")),
        "stress": _int(raw.get("stress")),
        "mood": _int(raw.get("mood")),
        "motivation": _int(raw.get("motivation")),
        "injury": _int(raw.get("injury")),
        "readiness_icu": _num(raw.get("readiness")),
        "weight_kg": _num(raw.get("weight")),
        "vo2max": _num(raw.get("vo2max")),
        "steps": _int(raw.get("steps")),
        "comments": _str(raw.get("comments")),
        "raw_json": raw,
    }
    return date_local, values


# ---------------------------------------------------------------------------- power curves


def power_curve_snapshot_values(curve: JsonDict, model: JsonDict | None) -> JsonDict:
    """``power_curve_snapshots`` columns from one ``DataCurve`` (+ optional ``PowerModel``)."""
    secs = [int(s) for s in curve.get("secs") or []]
    watts = [_num(v) for v in curve.get("values") or []]
    weight = _num(curve.get("weight"))
    w_kg = [round(w / weight, 3) if (w is not None and weight) else None for w in watts]
    m = model or {}
    return {
        "durations_s": secs,
        "watts": watts,
        "w_kg": w_kg,
        "cp": _num(m.get("criticalPower")),
        "w_prime": _num(m.get("wPrime")),
        "p_max": _num(m.get("pMax")),
        "eftp_icu": _num(m.get("ftp")),
        "raw_json": {"curve": curve, "mmp_model": model},
    }


# ---------------------------------------------------------------------------------- events


def event_row(raw: JsonDict) -> JsonDict:
    """``icu_events`` columns from an icu ``Event``."""
    return {
        "id": int(raw["id"]),
        "category": _str(raw.get("category")),
        "start_date_local": _str(raw.get("start_date_local")),
        "end_date_local": _str(raw.get("end_date_local")),
        "name": _str(raw.get("name")),
        "description": _str(raw.get("description")),
        "type": _str(raw.get("type")),
        "external_id": _str(raw.get("external_id")),
        "icu_training_load": _num(raw.get("icu_training_load")),
        "training_availability": _str(raw.get("training_availability")),
        "max_training_time": _int(raw.get("max_training_time")),
        "raw_json": raw,
    }
