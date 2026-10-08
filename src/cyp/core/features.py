"""Feature flags: one registry, resolved per profile (ADR-0008, docs/09-features.md).

Every optional capability the autopilot runs is a :class:`Feature` in :data:`REGISTRY`. A
profile switches them in ``athlete.yaml``::

    features:
      sync.strava: false
      notify.macos: true

Resolution order (later wins): registry default -> legacy env switch (``STRAVA_ENABLED``) ->
``athlete.yaml`` ``features`` -> ``CYP_FEATURES`` env (``"id=on,id=off"``, for one-off runs).
A feature whose ``requires`` are not all on is off, with the reason recorded.

Adding a feature: add it here, document it in docs/09-features.md (a test checks), and guard
the code path with ``ctx.features().enabled("<id>")``. Never read flags from anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal

from cyp.core.errors import ConfigError

Stage = Literal["sync", "analysis", "report", "plan", "publish", "notify"]
Source = Literal["default", "legacy-env", "athlete.yaml", "CYP_FEATURES"]


@dataclass(frozen=True)
class Feature:
    """One switchable capability."""

    id: str
    stage: Stage
    default: bool
    title_zh: str
    summary: str
    requires: tuple[str, ...] = ()


REGISTRY: tuple[Feature, ...] = (
    Feature(
        "sync.intervals",
        "sync",
        True,
        "同步 intervals.icu",
        "Pull activities, streams, wellness and sport settings from intervals.icu.",
    ),
    Feature(
        "sync.strava",
        "sync",
        True,
        "同步 Strava",
        "Strava segments/PRs and fallback streams (needs Strava OAuth).",
        requires=("sync.intervals",),
    ),
    Feature(
        "analysis.rides",
        "analysis",
        True,
        "騎乘分析",
        "Per-ride metrics, trends (PMC, CP/W', FTP evidence, limiters) and readiness.",
    ),
    Feature(
        "report.daily",
        "report",
        True,
        "每日報告",
        "Daily zh-TW report under the profile's data/reports/daily.",
        requires=("analysis.rides",),
    ),
    Feature(
        "report.weekly",
        "report",
        True,
        "每週報告",
        "Weekly review report, built by the autopilot on Mondays.",
        requires=("analysis.rides",),
    ),
    Feature(
        "plan.horizon",
        "plan",
        True,
        "滾動排課",
        "Replan the rolling horizon (season skeleton, guardrails, daily adaptation).",
    ),
    Feature(
        "plan.climb_routes",
        "plan",
        True,
        "爬坡路線建議",
        "Name a local climb from athlete.yaml for long outdoor work steps.",
        requires=("plan.horizon",),
    ),
    Feature(
        "plan.athlete_rules",
        "plan",
        True,
        "年齡與健康規則",
        "birth_year / health_flags set safer defaults: 2:1 recovery, ramp and intensity caps, "
        "no maximal tests for flagged health answers; with plan.goal_menus, a lighter second "
        "hard day for athletes 60+. Off: the planner never sees those answers.",
        requires=("plan.horizon",),
    ),
    Feature(
        "plan.long_ride_progression",
        "plan",
        True,
        "長騎逐步拉長",
        "With a distance goal and long_ride_max_minutes: the long ride grows 15 min per loading "
        "week (planned exactly that long), one over-distance ride per block.",
        requires=("plan.horizon",),
    ),
    Feature(
        "plan.goal_menus",
        "plan",
        True,
        "依目標選課",
        "Workout menus by goal emphasis (FTP vs distance), with alternatives so limiter bias "
        "and planner.intensity decide; planner.hit_days. Off: both preferences are ignored.",
        requires=("plan.horizon",),
    ),
    Feature(
        "plan.variety",
        "plan",
        True,
        "課表輪替",
        "Rotate equally ranked workout alternatives week by week (still deterministic).",
        requires=("plan.horizon",),
    ),
    Feature(
        "readiness.ride_feel",
        "analysis",
        True,
        "騎後感受納入準備度",
        "Post-ride RPE / feel (intervals.icu or the web 騎完感受, per field the web wins) of "
        "the longest rated ride adjusts the next day's readiness (algo readiness_v1.1).",
        requires=("analysis.rides",),
    ),
    Feature(
        "publish.calendar",
        "publish",
        True,
        "寫入 intervals.icu 行事曆",
        "Diff the plan against the icu calendar; writes only when planner.mode is apply.",
        requires=("plan.horizon",),
    ),
    Feature(
        "api.calendar_write",
        "publish",
        True,
        "網頁可寫入行事曆",
        "The web UI's 'run and write' button works for this profile (still needs apply mode, "
        "a confirmation and the write guard).",
        requires=("publish.calendar",),
    ),
    Feature(
        "strava.write_description",
        "publish",
        False,
        "寫入 Strava 說明",
        "Write a ride's RIDE.LOG into its Strava description (needs the activity:write scope "
        "and a confirmation; the athlete's own text is kept).",
        requires=("sync.strava",),
    ),
    Feature(
        "notify.macos",
        "notify",
        True,
        "macOS 失敗通知",
        "Show a macOS notification when a run fails (no-op on other systems).",
    ),
)

FEATURES: dict[str, Feature] = {f.id: f for f in REGISTRY}


_WORDS = {
    "on": True,
    "off": False,
    "true": True,
    "false": False,
    "1": True,
    "0": False,
    "yes": True,
    "no": False,
}


def check_ids(ids: Iterable[object], *, where: str) -> None:
    """Raise :class:`ConfigError` naming every unknown feature id in ``ids``."""
    unknown = sorted(str(i) for i in ids if str(i) not in FEATURES)
    if unknown:
        raise ConfigError(
            f"unknown feature(s) in {where}: {', '.join(unknown)}; known: {', '.join(FEATURES)}"
        )


def parse_env(value: str) -> dict[str, bool]:
    """``"a=on,b=off"`` (also true/false/1/0/yes/no) -> ``{"a": True, "b": False}``.

    Raises:
        ConfigError: malformed entry or unknown id.
    """
    out: dict[str, bool] = {}
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        key, sep, flag = item.partition("=")
        word = flag.strip().lower()
        if not sep or word not in _WORDS:
            raise ConfigError(f"CYP_FEATURES entry {item!r}: expected <id>=on|off")
        out[key.strip()] = _WORDS[word]
    check_ids(out, where="CYP_FEATURES")
    return out


@dataclass(frozen=True)
class FeatureState:
    """Resolved value of one feature and why."""

    feature: Feature
    on: bool
    source: Source
    #: Set when the feature was switched on but a required feature is off.
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureSet:
    """Resolved flags for one profile; the only way code asks "is X on?"."""

    states: Mapping[str, FeatureState] = field(default_factory=dict)

    def enabled(self, feature_id: str) -> bool:
        """Whether ``feature_id`` is on.

        Raises:
            KeyError: unknown id (a typo must fail loudly, not silently read as off).
        """
        if feature_id not in FEATURES:
            raise KeyError(f"unknown feature {feature_id!r}")
        return self.states[feature_id].on

    def __iter__(self) -> Iterator[FeatureState]:
        """States in registry order."""
        return iter(self.states[f.id] for f in REGISTRY)

    def as_dict(self) -> dict[str, bool]:
        """``{id: on}`` in registry order."""
        return {s.feature.id: s.on for s in self}


def resolve(
    *,
    config: Mapping[str, bool] | None = None,
    env: str = "",
    legacy: Mapping[str, bool] | None = None,
) -> FeatureSet:
    """Resolve every registered feature (see the module docstring for the order).

    Raises:
        ConfigError: unknown ids in ``config`` or ``env``.
    """
    config = dict(config or {})
    check_ids(config, where="athlete.yaml features")
    env_values = parse_env(env) if env else {}
    raw: dict[str, tuple[bool, Source]] = {}
    for f in REGISTRY:
        value = f.default
        source: Source = "default"
        layers: tuple[tuple[Mapping[str, bool], Source], ...] = (
            (legacy or {}, "legacy-env"),
            (config, "athlete.yaml"),
            (env_values, "CYP_FEATURES"),
        )
        for values, src in layers:
            if f.id in values:
                value, source = bool(values[f.id]), src
        raw[f.id] = (value, source)
    states: dict[str, FeatureState] = {}
    for f in REGISTRY:  # registry order lists requirements before dependants
        value, source = raw[f.id]
        blocked = tuple(r for r in f.requires if not states[r].on)
        states[f.id] = FeatureState(f, value and not blocked, source, blocked if value else ())
    return FeatureSet(states)
