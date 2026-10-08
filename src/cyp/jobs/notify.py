"""Desktop notifications for scheduled runs (feature ``notify.macos``)."""

from __future__ import annotations

import subprocess
import sys

from cyp.logging import get_logger

log = get_logger(__name__)


def applescript(title: str, message: str) -> str:
    """AppleScript for a notification; quotes and backslashes escaped."""

    def q(text: str) -> str:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return f"display notification {q(message)} with title {q(title)}"


def notify_macos(title: str, message: str) -> bool:
    """Show a macOS notification; ``False`` (no error) elsewhere or when it fails."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", applescript(title, message)],
            check=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notify.failed", error=str(exc))
        return False
    return True
