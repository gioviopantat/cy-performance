"""Feature flag registry and resolution (ADR-0008)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cyp.core.errors import ConfigError
from cyp.core.features import FEATURES, REGISTRY, parse_env, resolve
from cyp.settings import get_settings, load_athlete_config, resolve_features, with_feature_switches


def test_registry_is_ordered_and_requirements_exist() -> None:
    seen: set[str] = set()
    for f in REGISTRY:
        assert all(r in seen for r in f.requires), f"{f.id}: requirements must come first"
        seen.add(f.id)
    assert len(FEATURES) == len(REGISTRY)


def test_defaults_and_precedence() -> None:
    fs = resolve()
    assert fs.enabled("publish.calendar") and fs.enabled("notify.macos")
    fs = resolve(
        legacy={"sync.strava": False},
        config={"sync.strava": True, "notify.macos": True},
        env="notify.macos=off",
    )
    assert fs.enabled("sync.strava") and fs.states["sync.strava"].source == "athlete.yaml"
    assert not fs.enabled("notify.macos") and fs.states["notify.macos"].source == "CYP_FEATURES"


def test_requirements_switch_dependants_off() -> None:
    fs = resolve(config={"plan.horizon": False})
    assert not fs.enabled("publish.calendar")
    assert fs.states["publish.calendar"].blocked_by == ("plan.horizon",)
    assert not fs.enabled("plan.climb_routes")


def test_unknown_ids_fail_loudly() -> None:
    with pytest.raises(ConfigError, match="nope"):
        resolve(config={"nope": True})
    with pytest.raises(ConfigError):
        parse_env("publish.calendar=maybe")
    with pytest.raises(KeyError):
        resolve().enabled("typo.feature")
    assert parse_env(" sync.strava=0, notify.macos=yes ,") == {
        "sync.strava": False,
        "notify.macos": True,
    }


def test_athlete_yaml_features_validated(athlete_yaml: Path) -> None:
    data = yaml.safe_load(athlete_yaml.read_text(encoding="utf-8"))
    data["features"] = {"notify.macos": True}
    athlete_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_athlete_config(athlete_yaml).features == {"notify.macos": True}
    data["features"] = {"bogus": True}
    athlete_yaml.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match="bogus"):
        load_athlete_config(athlete_yaml)


def test_legacy_strava_switch_and_bridge(
    monkeypatch: pytest.MonkeyPatch, athlete_yaml: Path
) -> None:
    monkeypatch.setenv("CYP_ATHLETE_CONFIG", str(athlete_yaml))
    monkeypatch.setenv("STRAVA_ENABLED", "false")
    s = get_settings()
    assert not resolve_features(s, None).enabled("sync.strava")
    monkeypatch.setenv("STRAVA_ENABLED", "true")
    monkeypatch.setenv("CYP_FEATURES", "sync.strava=off")
    s = with_feature_switches(get_settings())
    assert s.strava_enabled is False
    assert with_feature_switches(s) is s  # idempotent
