"""macOS launchd agents: the daily autopilot (ADR-0006 L3) and the ride watcher.

``StartCalendarInterval`` jobs that were missed while the Mac slept run once on wake, so a
closed lid at 05:30 delays the run instead of skipping it. The autopilot agent runs
``<venv python> -m cyp.cli run --all`` from the repo directory; the watch agent runs
``cyp watch --all`` every ``StartInterval`` seconds (docs/specs/ride-report.md).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cyp.core.errors import CypError

LABEL = "com.cy-performance.autopilot"
WATCH_LABEL = "com.cy-performance.watch"
WATCH_INTERVAL_S = 600


@dataclass(frozen=True)
class AgentSpec:
    """What the launch agent runs and when."""

    workdir: Path
    hour: int = 5
    minute: int = 30
    python: str = sys.executable
    label: str = LABEL
    #: ``cyp`` arguments the agent runs.
    args: tuple[str, ...] = ("run", "--all")
    #: Run every N seconds instead of daily at ``hour:minute``.
    interval_s: int | None = None

    @property
    def log_path(self) -> Path:
        """stdout/stderr of the agent (outside the repo, survives profile moves)."""
        name = self.label.rsplit(".", 1)[-1]
        return Path.home() / "Library" / "Logs" / "cy-performance" / f"{name}.log"

    @property
    def plist_path(self) -> Path:
        """``~/Library/LaunchAgents/<label>.plist``."""
        return Path.home() / "Library" / "LaunchAgents" / f"{self.label}.plist"


def watch_spec(workdir: Path, interval_s: int = WATCH_INTERVAL_S) -> AgentSpec:
    """The ride watcher agent: ``cyp watch --all`` every ``interval_s`` seconds."""
    return AgentSpec(
        workdir=workdir, label=WATCH_LABEL, args=("watch", "--all"), interval_s=interval_s
    )


def build_plist(spec: AgentSpec) -> bytes:
    """The agent plist (XML)."""
    if not (0 <= spec.hour <= 23 and 0 <= spec.minute <= 59):
        raise ValueError(f"invalid time {spec.hour:02d}:{spec.minute:02d}")
    if spec.interval_s is not None and spec.interval_s < 60:
        raise ValueError(f"interval {spec.interval_s}s is below 60s")
    when: dict[str, object] = (
        {"StartInterval": spec.interval_s}
        if spec.interval_s is not None
        else {"StartCalendarInterval": {"Hour": spec.hour, "Minute": spec.minute}}
    )
    return plistlib.dumps(
        {
            "Label": spec.label,
            "ProgramArguments": [spec.python, "-m", "cyp.cli", *spec.args],
            "WorkingDirectory": str(spec.workdir),
            **when,
            "StandardOutPath": str(spec.log_path),
            "StandardErrorPath": str(spec.log_path),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            "RunAtLoad": False,
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, check=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(spec: AgentSpec) -> Path:
    """Write the plist and (re)load it into the user's GUI domain.

    Raises:
        CypError: not macOS, or launchctl refused the agent.
    """
    if sys.platform != "darwin":
        raise CypError("launchd scheduling is macOS only; use cron/systemd elsewhere")
    if (spec.workdir / ".git").is_file():
        raise CypError(
            f"{spec.workdir} is a git worktree; install from the main checkout so the agent "
            "does not point at a directory that is later removed"
        )
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.plist_path.parent.mkdir(parents=True, exist_ok=True)
    spec.plist_path.write_bytes(build_plist(spec))
    _launchctl("bootout", f"{_domain()}/{spec.label}")  # ignore "not loaded"
    for _attempt in range(5):  # bootout is asynchronous: bootstrap may race it briefly
        res = _launchctl("bootstrap", _domain(), str(spec.plist_path))
        if res.returncode == 0:
            break
        time.sleep(1)
    if res.returncode != 0:
        raise CypError(f"launchctl bootstrap failed: {res.stderr.strip() or res.returncode}")
    return spec.plist_path


def uninstall(label: str = LABEL) -> bool:
    """Unload and delete the agent; ``True`` when a plist was removed."""
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    _launchctl("bootout", f"{_domain()}/{label}")
    if plist.is_file():
        plist.unlink()
        return True
    return False


def status(label: str = LABEL) -> str | None:
    """``launchctl print`` output for the agent, or ``None`` when it is not loaded."""
    if sys.platform != "darwin":
        return None
    res = _launchctl("print", f"{_domain()}/{label}")
    return res.stdout if res.returncode == 0 else None
