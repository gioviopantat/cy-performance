"""Known problems in the athlete's recorded data (``data_quality`` in athlete.yaml), pure.

A :class:`PowerRule` marks a power meter (or every meter, ``serial=None``) as unreliable up to
and including ``until``. Rides matching a rule are ``power_unreliable``: their HR / time /
elevation stay valid, but their power must not feed power curves, CP/W′ fits, FTP evidence,
climb power trends, baselines or limiters (docs/04 §7).

Matching (:func:`unreliable_rule`) for a ride with measured power on ``day``:

1. ``day > rule.until`` -> not matched.
2. ``rule.serial is None`` -> matched (any meter).
3. The ride recorded a meter serial (icu ``power_meter_serial``) -> matched iff equal.
4. No serial recorded (Strava-only / icu without the field) -> matched iff the ride's
   ``gear_id`` is a bike on which that serial was recorded on/before ``until``
   (:func:`infer_meter_gears`). Rides with neither serial nor a known bike (e.g. virtual
   platform uploads without gear) are not flagged: there is no evidence which meter it was.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PowerRule:
    """Power from ``serial`` (``None`` = any meter) is unreliable on/before ``until``."""

    until: dt.date
    serial: str | None = None
    reason_zh: str = ""

    def to_json(self) -> dict[str, str | None]:
        """JSON-safe dict (stored in ``activity_metrics.comparison``)."""
        return {
            "until": self.until.isoformat(),
            "power_meter_serial": self.serial,
            "reason_zh": self.reason_zh,
        }


@dataclass(frozen=True)
class DataQuality:
    """Resolved ``data_quality`` section (hashable: used as a cache key)."""

    power_rules: tuple[PowerRule, ...] = ()
    #: Daily loads up to this day use our re-analysed TSS instead of icu's ledger
    #: (see :meth:`cyp.dataset.Dataset.with_power_fix`).
    load_fix_until: dt.date | None = None

    @property
    def empty(self) -> bool:
        """Nothing to correct."""
        return not self.power_rules and self.load_fix_until is None


def normalise_serial(value: object) -> str | None:
    """Icu stores serials as strings, YAML may parse them as ints: compare as trimmed text."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def infer_meter_gears(
    rides: Iterable[tuple[dt.date, str | None, str | None]], rules: Iterable[PowerRule]
) -> dict[str, frozenset[str]]:
    """``serial -> gear ids`` it was recorded on, from ``(day, serial, gear_id)`` triples.

    Only rides on/before the serial's (latest) rule ``until`` count, so a meter moved to
    another bike later does not taint that bike's earlier rides.
    """
    until: dict[str, dt.date] = {}
    for r in rules:
        if r.serial is not None:
            until[r.serial] = max(r.until, until.get(r.serial, r.until))
    out: dict[str, set[str]] = {}
    for day, serial, gear in rides:
        s = normalise_serial(serial)
        if s is None or gear is None or s not in until or day > until[s]:
            continue
        out.setdefault(s, set()).add(gear)
    return {k: frozenset(v) for k, v in out.items()}


def unreliable_rule(
    rules: Iterable[PowerRule],
    day: dt.date,
    serial: str | None,
    gear_id: str | None,
    meter_gears: Mapping[str, frozenset[str]],
) -> PowerRule | None:
    """First rule that marks a ride's power as unreliable (see module docstring)."""
    s = normalise_serial(serial)
    for r in rules:
        if day > r.until:
            continue
        if r.serial is None:
            return r
        if s is not None:
            if s == r.serial:
                return r
            continue
        if gear_id is not None and gear_id in meter_gears.get(r.serial, frozenset()):
            return r
    return None


def unreliable_text_zh(rule: PowerRule) -> str:
    """One-line zh-TW statement used in ride Explanations."""
    meter = f"功率計 {rule.serial}" if rule.serial else "所有功率計"
    reason = f"：{rule.reason_zh}" if rule.reason_zh else ""
    return (
        f"此趟功率計數據不可信{reason}（{meter}，{rule.until.isoformat()} 以前）。"
        "功率不列入功率曲線、CP/W′、FTP 證據、爬坡功率與限制因子；心率、時間、爬升仍有效。"
    )
