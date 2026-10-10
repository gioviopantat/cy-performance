"""Run the autopilot for many profiles, one subprocess each (ADR-0006 L2).

Process isolation keeps the per-process caches (``dataset.CACHE``), DB engines and logging of
one athlete away from another's, and a crash in one profile cannot stop the others. Each child
is ``python -m cyp.cli --profile <slug> run --json``; its last stdout line is the JSON report.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cyp.core.errors import ConfigError, CypError
from cyp.jobs.notify import notify_macos
from cyp.services.autopilot import AutopilotReport, StageResult, record_failure, run_autopilot
from cyp.services.context import AppContext
from cyp.settings import Settings, load_athlete_config, profile_settings, resolve_features
from cyp.store.migrate import upgrade_head

LOCK_NAME = ".autopilot.lock"
CHILD_TIMEOUT_S = 60 * 60
#: Host whose DNS answer means "the network is up" after the Mac wakes for the 05:30 run.
NETWORK_PROBE_HOST = "intervals.icu"
NETWORK_WAIT_S = 15 * 60


class RunLockedError(CypError):
    """Another autopilot run holds this profile's lock."""


@contextmanager
def profile_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive, non-blocking lock on ``data_dir/.autopilot.lock`` (released on exit/crash).

    Raises:
        RunLockedError: another process holds it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / LOCK_NAME).open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLockedError(f"another run is in progress for {data_dir}") from exc
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@dataclass
class ChildResult:
    """Outcome of one profile's subprocess."""

    profile: str
    exit_code: int
    report: dict[str, Any] | None
    stderr_tail: str

    @property
    def ok(self) -> bool:
        """Exit 0 and a report that says ok."""
        return self.exit_code == 0 and bool(self.report and self.report.get("ok"))


def run_profile(settings: Settings, *, no_write: bool = False) -> AutopilotReport:
    """One profile's unattended run: lock -> migrate -> autopilot -> notify on failure.

    Unattended runs migrate themselves: a ``git pull`` with a new migration must not stop
    tomorrow's plan (Alembic upgrades are additive). Always returns a report; problems before
    the autopilot starts (lock held, migration error) become a failed ``setup`` stage.
    """
    for path in settings.data_layout:
        path.mkdir(parents=True, exist_ok=True)
    profile = settings.cyp_profile or "default"
    try:
        with profile_lock(settings.cyp_data_dir):
            upgrade_head(settings.cyp_db_url)
            ctx = AppContext.from_settings(settings)
            try:
                report = run_autopilot(ctx, allow_write=not no_write)
            finally:
                ctx.close()
    except RunLockedError as exc:
        report = AutopilotReport(profile, _today(settings), "unknown", [_failed("lock", exc)])
        _leave_evidence(settings, report)
        return report  # the run holding the lock notifies for itself
    except Exception as exc:  # noqa: BLE001 - the scheduler must always get a report
        report = AutopilotReport(profile, _today(settings), "unknown", [_failed("setup", exc)])
        _leave_evidence(settings, report)
    if not report.ok:
        failed = ", ".join(s.name for s in report.stages if s.status == "failed")
        notify_failure(settings, f"autopilot failed: {failed}")
    return report


def _leave_evidence(settings: Settings, report: AutopilotReport) -> None:
    """``job_runs`` row + saved report for a run that never reached the autopilot."""
    try:
        ctx = AppContext.from_settings(settings)
    except Exception:  # noqa: BLE001 - best effort
        return
    try:
        record_failure(ctx, report, ctx.now_local())
    finally:
        ctx.close()


def notify_failure(settings: Settings, message: str) -> None:
    """MacOS notification if this profile's ``notify.macos`` is on (defaults when unreadable)."""
    try:
        cfg = load_athlete_config(settings.cyp_athlete_config)
    except ConfigError:
        cfg = None
    try:
        on = resolve_features(settings, cfg).enabled("notify.macos")
    except ConfigError:
        on = True
    if on:
        notify_macos(f"cy-performance: {settings.cyp_profile or 'default'}", message)


def _failed(stage: str, exc: Exception) -> StageResult:
    return StageResult(stage, "failed", f"{type(exc).__name__}: {exc}")


def _today(settings: Settings) -> dt.date:
    from cyp.core.timeutil import local_date, now_utc

    return local_date(now_utc(), settings.cyp_timezone)


def wait_for_network(
    host: str = NETWORK_PROBE_HOST,
    timeout_s: float = NETWORK_WAIT_S,
    *,
    resolve: Callable[[str], object] = lambda h: socket.getaddrinfo(h, 443),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until ``host`` resolves (a Mac woken from sleep has no DNS for a while).

    Returns False after ``timeout_s``; the caller runs anyway so the failure leaves evidence.
    """
    deadline = clock() + timeout_s
    delay = 5.0
    while True:
        try:
            resolve(host)
            return True
        except OSError:
            if clock() + delay > deadline:
                return False
            sleep(delay)
            delay = min(delay * 2, 60.0)


def run_all(profiles_dir: Path, slugs: Sequence[str], *, no_write: bool) -> list[ChildResult]:
    """Every profile in its own process; notifies for children that crashed without a report.

    Waits for the network first (docs/runbooks/network.md).
    """
    wait_for_network()
    results = run_profiles(slugs, extra=["--no-write"] if no_write else [], cwd=Path.cwd())
    for r in results:
        if r.report is None:
            try:
                settings = profile_settings(r.profile, profiles_dir)
            except ConfigError:
                continue
            notify_failure(settings, f"run crashed (exit {r.exit_code})")
    return results


def child_command(slug: str, extra: Sequence[str] = ()) -> list[str]:
    """Argv for one profile's run (same interpreter, so the same venv)."""
    return [sys.executable, "-m", "cyp.cli", "--profile", slug, "run", "--json", *extra]


def run_profiles(
    slugs: Sequence[str], *, extra: Sequence[str] = (), cwd: Path | None = None
) -> list[ChildResult]:
    """Run each profile sequentially in its own process; never raises for a child failure."""
    results: list[ChildResult] = []
    for slug in slugs:
        try:
            proc = subprocess.run(
                child_command(slug, extra),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=CHILD_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results.append(ChildResult(slug, 124, None, f"timed out after {CHILD_TIMEOUT_S}s"))
            continue
        results.append(
            ChildResult(slug, proc.returncode, _last_json(proc.stdout), proc.stderr[-2000:])
        )
    return results


def _last_json(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None
