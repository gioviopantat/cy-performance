"""Regression tests for the 2026-10-07 safety review of profiles / autopilot / publishing."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from cyp.cli import app
from cyp.core.errors import ConfigError
from cyp.ingest.intervals.client import BASE_URL
from cyp.jobs.runner import profile_lock, run_profile
from cyp.jobs.sync import build_intervals_syncer
from cyp.profiles import FilesystemProfileStore, ProfileMeta, set_planner_mode
from cyp.services import profiles as svc
from cyp.settings import Settings, load_athlete_config, profile_settings
from cyp.store.db import make_engine, session_factory
from cyp.store.models import JobRun
from cyp.store.runs import job_run

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_profile_leak() -> Iterator[None]:
    yield
    os.environ.pop("CYP_PROFILE", None)


def _profile(
    tmp_path: Path, athlete_yaml: Path, slug: str = "dad", icu: str | None = "i42"
) -> Path:
    store = FilesystemProfileStore(tmp_path / "profiles")
    store.create(
        ProfileMeta(slug=slug, display_name=slug, icu_athlete_id=icu, created=dt.date.today()),
        env={"INTERVALS_API_KEY": "k"},
        athlete_yaml=athlete_yaml.read_text(encoding="utf-8"),
    )
    return store.root / slug


# ------------------------------------------------------------------ set_planner_mode (#2)


def test_planner_first_line_keeps_every_planner_setting(tmp_path: Path, athlete_yaml: Path) -> None:
    data = yaml.safe_load(athlete_yaml.read_text(encoding="utf-8"))
    planner = data.pop("planner")
    target = tmp_path / "a.yaml"
    target.write_text(
        yaml.safe_dump({"planner": planner}, sort_keys=False)
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    set_planner_mode(target, "apply")
    cfg = load_athlete_config(target)
    assert cfg.planner.mode == "apply"
    assert cfg.planner.ramp_cap == planner["ramp_cap"]
    assert cfg.planner.tsb_floor == planner["tsb_floor"]


def test_planner_mode_refuses_duplicate_sections(tmp_path: Path, athlete_yaml: Path) -> None:
    target = tmp_path / "a.yaml"
    target.write_text(athlete_yaml.read_text(encoding="utf-8") + "\nplanner:\n  mode: propose\n")
    before = target.read_text()
    with pytest.raises(ConfigError, match="more than one"):
        set_planner_mode(target, "apply")
    assert target.read_text() == before


# ------------------------------------------------------------------- write guard (#5)


def test_profile_without_icu_id_never_writes(tmp_path: Path, athlete_yaml: Path) -> None:
    from cyp.services.context import AppContext
    from cyp.services.publish import WriteGuardError, check_write_guard
    from cyp.store.migrate import upgrade_head

    _profile(tmp_path, athlete_yaml, icu=None)
    settings = profile_settings("dad", tmp_path / "profiles")
    settings.cyp_data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_head(settings.cyp_db_url)
    ctx = AppContext.from_settings(settings)
    try:
        with pytest.raises(WriteGuardError, match="unknown target athlete"):
            check_write_guard(ctx, "k", client_factory=lambda k, a: None)  # type: ignore[arg-type,return-value]
    finally:
        ctx.close()


def test_sync_refuses_a_key_of_another_athlete(tmp_path: Path, athlete_yaml: Path) -> None:
    _profile(tmp_path, athlete_yaml, icu="i42")
    settings = profile_settings("dad", tmp_path / "profiles")
    with respx.mock(base_url=BASE_URL) as router:
        router.get("/athlete/0").mock(return_value=httpx.Response(200, json={"id": "i7"}))
        with pytest.raises(ConfigError, match="belongs to i7"):
            build_intervals_syncer(
                settings, session_factory(make_engine("sqlite://")), streams=False
            )


def test_sync_fails_closed_without_an_icu_id(tmp_path: Path, athlete_yaml: Path) -> None:
    """Review 2026-10-08: a profile without icu_athlete_id synced any key's athlete."""
    _profile(tmp_path, athlete_yaml, icu=None)
    settings = profile_settings("dad", tmp_path / "profiles")
    with pytest.raises(ConfigError, match="no icu_athlete_id"):
        build_intervals_syncer(settings, session_factory(make_engine("sqlite://")), streams=False)


def test_sync_refuses_a_configured_athlete_of_someone_else(
    tmp_path: Path, athlete_yaml: Path
) -> None:
    _profile(tmp_path, athlete_yaml, icu="i42")
    settings = profile_settings("dad", tmp_path / "profiles", intervals_athlete_id="i7")
    with pytest.raises(ConfigError, match="INTERVALS_ATHLETE_ID=i7"):
        build_intervals_syncer(settings, session_factory(make_engine("sqlite://")), streams=False)


# --------------------------------------------------------------- run_profile (#6)


def test_locked_and_broken_profiles_still_report(tmp_path: Path, athlete_yaml: Path) -> None:
    root = _profile(tmp_path, athlete_yaml)
    settings = profile_settings("dad", tmp_path / "profiles")
    with profile_lock(settings.cyp_data_dir):
        locked = run_profile(settings, no_write=True)
    assert not locked.ok and locked.stages[0].name == "lock"
    reports = settings.reports_dir / "autopilot"
    assert len(list(reports.glob("*.json"))) == 1  # review 2026-10-08: lock left no evidence

    (root / "athlete.yaml").write_text("athlete: {}\n")  # invalid
    broken = run_profile(settings, no_write=True)
    assert [s.name for s in broken.stages] == ["config"] and not broken.ok
    engine = make_engine(settings.cyp_db_url)
    with session_factory(engine)() as s:
        rows = s.scalars(select(JobRun).where(JobRun.job == "autopilot")).all()
    engine.dispose()
    # config: a row; lock: report file only here (a fresh profile has no schema yet, and the
    # lock holder is mid-run, so we never migrate under it)
    assert [r.status for r in rows] == ["failed"]
    with profile_lock(settings.cyp_data_dir):  # schema exists now: the lock failure gets a row
        run_profile(settings, no_write=True)
    engine = make_engine(settings.cyp_db_url)
    with session_factory(engine)() as s:
        assert len(s.scalars(select(JobRun).where(JobRun.job == "autopilot")).all()) == 2
    engine.dispose()


def test_setup_failure_leaves_a_job_run(
    tmp_path: Path, athlete_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cyp.jobs import runner as runner_mod

    _profile(tmp_path, athlete_yaml)
    settings = profile_settings("dad", tmp_path / "profiles")
    real = runner_mod.upgrade_head

    def broken(url: str) -> None:
        real(url)  # the schema exists, then the "migration" fails
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(runner_mod, "upgrade_head", broken)
    report = run_profile(settings, no_write=True)
    assert [s.name for s in report.stages] == ["setup"] and not report.ok
    engine = make_engine(settings.cyp_db_url)
    with session_factory(engine)() as s:
        row = s.scalars(select(JobRun).where(JobRun.job == "autopilot")).one()
    engine.dispose()
    assert row.status == "failed" and list((settings.reports_dir / "autopilot").glob("*.json"))


# ------------------------------------------------------------- CLI write paths (#3, #8)


def test_plan_apply_refuses_a_shifted_date() -> None:
    res = runner.invoke(app, ["plan", "--apply", "--confirm-write", "--date", "2026-01-01"])
    assert res.exit_code == 2 and "drop --date" in res.output


def test_unknown_profile_is_a_clean_error() -> None:
    res = runner.invoke(app, ["--profile", "nosuch", "run", "--no-write"])
    assert res.exit_code == 2 and "not found" in res.output
    assert res.exception is None or isinstance(res.exception, SystemExit)


# ------------------------------------------------------------------------- adopt (#7)


def test_adopt_refuses_to_overwrite_and_copies_foreign_symlinks(
    tmp_path: Path, athlete_yaml: Path
) -> None:
    (tmp_path / ".env").write_text("INTERVALS_API_KEY=legacy\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    real = tmp_path / "elsewhere.yaml"
    real.write_text(athlete_yaml.read_text(encoding="utf-8"))
    (tmp_path / "config" / "athlete.yaml").symlink_to(real)
    store = FilesystemProfileStore(tmp_path / "profiles")
    (store.root / "me").mkdir(parents=True)
    (store.root / "me" / ".env").write_text("KEEP=1\n")
    with pytest.raises(ConfigError, match="refusing to overwrite"):
        svc.plan_adopt(store, "me", Settings())
    (store.root / "me" / ".env").unlink()
    plan = svc.plan_adopt(store, "me", Settings())
    profile = svc.adopt_legacy(store, plan, "Me")
    assert profile.athlete_config_path.is_file() and not profile.athlete_config_path.is_symlink()
    assert real.is_file()  # the symlink's target is copied, not moved away
    assert (profile.root / ".env").read_text() == "INTERVALS_API_KEY=legacy\n"


def test_adopt_refuses_a_db_outside_the_data_dir(tmp_path: Path, athlete_yaml: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "athlete.yaml").write_text(athlete_yaml.read_text(encoding="utf-8"))
    base = Settings(cyp_db_url=f"sqlite:///{tmp_path / 'other.sqlite'}")
    with pytest.raises(ConfigError, match="outside"):
        svc.plan_adopt(FilesystemProfileStore(tmp_path / "profiles"), "me", base)


# ------------------------------------------------------------------- profile list (#1b)


def test_profile_list_shows_the_last_run(tmp_path: Path, athlete_yaml: Path) -> None:
    from cyp.store.migrate import upgrade_head

    _profile(tmp_path, athlete_yaml)
    settings = profile_settings("dad", tmp_path / "profiles")
    settings.cyp_data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_head(settings.cyp_db_url)
    engine = make_engine(settings.cyp_db_url)
    with job_run("autopilot", session_factory(engine)):
        pass
    engine.dispose()
    res = runner.invoke(app, ["profile", "list", "--json"])
    row = json.loads(res.output)[0]
    assert row["last_run_status"] == "ok" and row["last_run"].startswith(str(dt.date.today().year))
    assert "unreadable" not in res.output


def test_publish_spike_uses_the_write_guard(
    tmp_path: Path, athlete_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 2026-10-08: the spike wrote to whatever calendar the key pointed at."""
    from cyp.store.migrate import upgrade_head

    monkeypatch.chdir(tmp_path)
    _profile(tmp_path, athlete_yaml, icu=None)
    settings = profile_settings("dad", tmp_path / "profiles")
    settings.cyp_data_dir.mkdir(parents=True, exist_ok=True)
    upgrade_head(settings.cyp_db_url)
    res = runner.invoke(
        app, ["--profile", "dad", "publish", "spike", "--date", "2030-01-01", "--confirm-write"]
    )
    assert res.exit_code == 2 and "unknown target athlete" in res.output
