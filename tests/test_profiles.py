"""Profiles: settings isolation, store, planner-mode edits (ADR-0006)."""

from __future__ import annotations

import datetime as dt
import shutil
import stat
from pathlib import Path

import pytest

from cyp.core.errors import ConfigError
from cyp.profiles import (
    FilesystemProfileStore,
    ProfileMeta,
    check_slug,
    set_planner_mode,
    write_env,
)
from cyp.settings import get_settings, load_athlete_config, profile_settings


def _make(store: FilesystemProfileStore, slug: str, yaml_src: Path, **env: str) -> None:
    store.create(
        ProfileMeta(slug=slug, display_name=slug, icu_athlete_id="i1", created=dt.date.today()),
        env={"INTERVALS_API_KEY": f"key-{slug}", **env},
        athlete_yaml=yaml_src.read_text(encoding="utf-8"),
    )


def test_profile_settings_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, athlete_yaml: Path
) -> None:
    (tmp_path / ".env").write_text("INTERVALS_API_KEY=root-key\nANTHROPIC_API_KEY=x\n")
    monkeypatch.setenv("INTERVALS_API_KEY", "process-key")
    monkeypatch.setenv("CYP_TIMEZONE", "Europe/Paris")
    store = FilesystemProfileStore(tmp_path / "profiles")
    _make(store, "dad", athlete_yaml)
    _make(store, "bare", athlete_yaml)
    (store.root / "bare" / ".env").write_text("STRAVA_ENABLED=false\n")

    s = profile_settings("dad", store.root)
    assert s.intervals_api_key.get_secret_value() == "key-dad"
    assert s.cyp_data_dir == (store.root / "dad" / "data").resolve()
    assert s.cyp_db_url.endswith("profiles/dad/data/cyp.sqlite")
    assert s.cyp_athlete_config == store.root / "dad" / "athlete.yaml"
    assert s.cyp_timezone == "Asia/Taipei"  # hermetic: nothing leaks from the process env
    assert s.cyp_features == "notify.macos=off"  # ... except the inheritable one-off overrides
    bare = profile_settings("bare", store.root)
    # credentials never leak from the process env or the root .env into a profile
    assert bare.intervals_api_key.get_secret_value() == ""
    assert bare.anthropic_api_key.get_secret_value() == ""
    with pytest.raises(ConfigError, match="not found"):
        profile_settings("nobody", store.root)
    for bad in ("../dad", "Dad", "a b"):
        with pytest.raises(ConfigError, match="invalid profile name"):
            profile_settings(bad, store.root)


def test_get_settings_selects_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, athlete_yaml: Path
) -> None:
    store = FilesystemProfileStore(tmp_path / "profiles")
    assert get_settings().cyp_profile == ""  # legacy layout without profiles
    _make(store, "me", athlete_yaml)
    _make(store, "dad", athlete_yaml)
    store.set_default("me")
    assert get_settings().cyp_profile == "me"
    monkeypatch.setenv("CYP_PROFILE", "dad")
    assert get_settings().intervals_api_key.get_secret_value() == "key-dad"


def test_store_list_get_and_env_mode(tmp_path: Path, athlete_yaml: Path) -> None:
    store = FilesystemProfileStore(tmp_path / "profiles")
    assert store.list() == [] and store.default() == ""
    _make(store, "b", athlete_yaml)
    _make(store, "a", athlete_yaml)
    assert [p.slug for p in store.list()] == ["a", "b"]
    p = store.get("a")
    assert p.meta.icu_athlete_id == "i1" and p.planner_mode() == "propose"
    assert stat.S_IMODE(p.env_path.stat().st_mode) == 0o600
    with pytest.raises(ConfigError, match="exists"):
        _make(store, "a", athlete_yaml)
    with pytest.raises(ConfigError):
        store.set_default("zzz")
    for bad in ("Dad", "1x", "a b", "", "x" * 40):
        with pytest.raises(ConfigError):
            check_slug(bad)


def test_set_planner_mode_keeps_comments(tmp_path: Path, athlete_yaml: Path) -> None:
    target = tmp_path / "a.yaml"
    shutil.copy(athlete_yaml, target)
    before = target.read_text(encoding="utf-8")
    set_planner_mode(target, "apply")
    after = target.read_text(encoding="utf-8")
    assert load_athlete_config(target).planner.mode == "apply"
    assert after.count("\n") == before.count("\n")
    assert [line for line in after.splitlines() if "#" in line] == [
        line for line in before.splitlines() if "#" in line
    ]
    set_planner_mode(target, "propose")
    assert target.read_text(encoding="utf-8") == before
    with pytest.raises(ConfigError):
        set_planner_mode(target, "yolo")


def test_set_planner_mode_without_mode_line(tmp_path: Path, athlete_yaml: Path) -> None:
    target = tmp_path / "a.yaml"
    text = athlete_yaml.read_text(encoding="utf-8")
    target.write_text(text.replace("  mode: propose\n", ""), encoding="utf-8")
    set_planner_mode(target, "apply")
    assert load_athlete_config(target).planner.mode == "apply"


def test_write_env_is_private(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env(path, {"A": "1", "B": "two"})
    assert path.read_text() == "A=1\nB=two\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
