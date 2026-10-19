"""CLI: profile add/adopt/promote/use, features, run, schedule plist (ADR-0006/0008)."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import Result
from typer.testing import CliRunner

from cyp.cli import app
from cyp.ingest.intervals.client import BASE_URL
from cyp.jobs.launchd import AgentSpec, build_plist
from cyp.jobs.notify import applescript
from cyp.jobs.runner import RunLockedError, child_command, profile_lock
from cyp.profiles import FilesystemProfileStore
from cyp.settings import load_athlete_config

runner = CliRunner()
ATHLETE = {
    "id": "i900001",
    "name": "Test Parent",
    "timezone": "Asia/Taipei",
    "icu_weight": 72.0,
    "sportSettings": [{"types": ["Ride", "VirtualRide"], "ftp": 210}],
}


@pytest.fixture(autouse=True)
def _no_profile_leak() -> Iterator[None]:
    yield
    os.environ.pop("CYP_PROFILE", None)  # the --profile callback exports it


def _add(tmp_path: Path, *extra: str) -> Result:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        router.get("/athlete/0").mock(return_value=httpx.Response(200, json=ATHLETE))
        return runner.invoke(
            app,
            [
                "profile",
                "add",
                "parent",
                "--key",
                "secret-key",
                "--goal-ftp",
                "230",
                "--availability",
                "tue=60,sat=180",
                "--backfill-days",
                "0",
                "--yes",
                *extra,
            ],
        )


def test_profile_add_creates_private_workspace(tmp_path: Path) -> None:
    res = _add(tmp_path)
    assert res.exit_code == 0, res.output
    p = FilesystemProfileStore(Path("profiles")).get("parent")
    assert p.meta.icu_athlete_id == "i900001" and p.meta.display_name == "parent"
    env = p.env_path.read_text()
    assert "INTERVALS_API_KEY=secret-key" in env and "INTERVALS_ATHLETE_ID=i900001" in env
    assert stat.S_IMODE(p.env_path.stat().st_mode) == 0o600
    cfg = load_athlete_config(p.athlete_config_path)
    assert cfg.athlete.ftp_w == 210 and cfg.goals[0].target == {"ftp": 230}
    assert cfg.availability.per_day["sat"] == 180 and cfg.availability.weekly_max_minutes == 240
    assert cfg.planner.mode == "propose" and cfg.features == {"sync.strava": False}
    assert cfg.season.start.weekday() == 0
    assert (p.data_dir / "cyp.sqlite").is_file()
    assert FilesystemProfileStore(Path("profiles")).default() == "parent"
    again = _add(tmp_path)
    assert again.exit_code == 2


def test_profile_add_rejects_mismatched_athlete_id(tmp_path: Path) -> None:
    (tmp_path / "profiles" / "parent").mkdir(parents=True)
    (tmp_path / "profiles" / "parent" / ".env").write_text("INTERVALS_ATHLETE_ID=i1\n")
    res = _add(tmp_path)
    assert res.exit_code == 2 and "belongs to i900001" in res.output


def test_promote_demote_use_list_show_features(tmp_path: Path) -> None:
    assert _add(tmp_path).exit_code == 0
    res = runner.invoke(app, ["profile", "promote", "parent", "--yes"])
    assert res.exit_code == 0, res.output
    p = FilesystemProfileStore(Path("profiles")).get("parent")
    assert p.planner_mode() == "apply"
    assert runner.invoke(app, ["profile", "demote", "parent"]).exit_code == 0
    assert p.planner_mode() == "propose"
    assert runner.invoke(app, ["profile", "use", "nobody"]).exit_code == 2
    listing = runner.invoke(app, ["profile", "list", "--json"])
    rows = json.loads(listing.output)
    assert rows[0]["slug"] == "parent" and rows[0]["default"]
    assert rows[0]["planner_mode"] == "propose"
    shown = runner.invoke(app, ["profile", "show", "parent"])
    assert "icu API key    : set" in shown.output and "secret-key" not in shown.output
    feats = runner.invoke(app, ["--profile", "parent", "features", "--json"])
    by_id = {f["id"]: f for f in json.loads(feats.output)}
    assert by_id["sync.strava"]["on"] is False and by_id["sync.strava"]["source"] == "athlete.yaml"


def test_adopt_moves_legacy_layout(tmp_path: Path, athlete_yaml: Path) -> None:
    (tmp_path / ".env").write_text("INTERVALS_API_KEY=legacy\n")
    (tmp_path / "data" / "streams").mkdir(parents=True)
    (tmp_path / "data" / "marker").write_text("x")
    (tmp_path / "config").mkdir()
    shutil.copy(athlete_yaml, tmp_path / "config" / "athlete.yaml")
    res = runner.invoke(app, ["profile", "adopt", "me", "--yes"])
    assert res.exit_code == 0, res.output
    root = tmp_path / "profiles" / "me"
    assert (root / "data" / "marker").is_file() and not (tmp_path / "data").exists()
    assert (root / ".env").read_text() == "INTERVALS_API_KEY=legacy\n"
    assert (root / "athlete.yaml").is_file() and not (tmp_path / "config" / "athlete.yaml").exists()
    assert FilesystemProfileStore(tmp_path / "profiles").default() == "me"
    assert runner.invoke(app, ["profile", "adopt", "me", "--yes"]).exit_code == 2


def test_adopt_with_symlinked_config(tmp_path: Path, athlete_yaml: Path) -> None:
    root = tmp_path / "profiles" / "me"
    root.mkdir(parents=True)
    shutil.copy(athlete_yaml, root / "athlete.yaml")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "athlete.yaml").symlink_to(Path("../profiles/me/athlete.yaml"))
    res = runner.invoke(app, ["profile", "adopt", "me", "--yes"])
    assert res.exit_code == 0, res.output
    assert not (tmp_path / "config" / "athlete.yaml").is_symlink()
    assert (root / "athlete.yaml").is_file()


def test_run_all_without_profiles_and_child_isolation(tmp_path: Path) -> None:
    res = runner.invoke(app, ["run", "--all"])
    assert res.exit_code == 2 and "no profiles" in res.output
    assert child_command("parent", ["--no-write"])[-5:] == [
        "--profile",
        "parent",
        "run",
        "--json",
        "--no-write",
    ]


def test_run_single_profile_reports_json(tmp_path: Path) -> None:
    assert _add(tmp_path).exit_code == 0
    env = {"CYP_FEATURES": "sync.intervals=off,notify.macos=off"}
    res = runner.invoke(app, ["--profile", "parent", "run", "--json"], env=env)
    report = json.loads(res.output.strip().splitlines()[-1])
    assert report["profile"] == "parent" and report["planner_mode"] == "propose"
    names = [s["name"] for s in report["stages"]]
    assert names == ["sync", "analyze", "report.daily", "report.weekly", "plan", "publish"]
    assert res.exit_code == (0 if report["ok"] else 1)


def test_run_all_spawns_one_child_per_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _add(tmp_path).exit_code == 0
    monkeypatch.setenv("CYP_FEATURES", "sync.intervals=off,publish.calendar=off,notify.macos=off")
    res = runner.invoke(app, ["run", "--all", "--json"])
    lines = [json.loads(line) for line in res.output.strip().splitlines()]
    assert [r["profile"] for r in lines] == ["parent"]
    assert lines[0]["stages"][-1] == {
        "name": "publish",
        "status": "skipped",
        "detail": "feature publish.calendar is off",
    }


def test_profile_lock_is_exclusive(tmp_path: Path) -> None:
    with profile_lock(tmp_path), pytest.raises(RunLockedError), profile_lock(tmp_path):
        pass
    with profile_lock(tmp_path):  # released after exit
        pass


def test_launchd_plist_and_notification_quoting(tmp_path: Path) -> None:
    spec = AgentSpec(workdir=tmp_path, hour=5, minute=30, python="/venv/bin/python")
    plist = plistlib.loads(build_plist(spec))
    assert plist["ProgramArguments"] == ["/venv/bin/python", "-m", "cyp.cli", "run", "--all"]
    assert plist["StartCalendarInterval"] == {"Hour": 5, "Minute": 30}
    assert plist["WorkingDirectory"] == str(tmp_path)
    with pytest.raises(ValueError):
        build_plist(AgentSpec(workdir=tmp_path, hour=25))
    assert applescript('a"b', "c\\d") == 'display notification "c\\\\d" with title "a\\"b"'


def test_profile_add_records_age_health_and_distance(tmp_path: Path) -> None:
    res = _add(tmp_path, "--birth-year", "1950", "--health", "none", "--distance-km", "160")
    assert res.exit_code == 0, res.output
    cfg = load_athlete_config(Path("profiles/parent/athlete.yaml"))
    assert cfg.athlete.birth_year == 1950 and cfg.athlete.health_flags == []
    distance = next(g for g in cfg.goals if g.kind == "distance")
    assert distance.target == {"km": 160} and cfg.availability.long_ride_max_minutes == 270


def test_profile_add_rejects_unknown_health_flags(tmp_path: Path) -> None:
    res = _add(tmp_path, "--health", "asthma")
    assert res.exit_code == 2 and "unknown health flag" in res.output
