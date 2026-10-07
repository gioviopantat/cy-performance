"""Personal data never enters the public repo (ADR-0006 §8).

Real athlete files live in ``profiles/<slug>/`` (git-ignored). If this fails, run
``git rm --cached <path>`` and move the file under ``profiles/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FILES = {"config/athlete.yaml", ".env"}
FORBIDDEN_DIRS = ("profiles/", "data/")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.stdout.splitlines()


def test_no_personal_files_tracked() -> None:
    bad = [p for p in _tracked() if p in FORBIDDEN_FILES or p.startswith(FORBIDDEN_DIRS)]
    assert not bad, f"personal files tracked in the public repo: {bad}; move them to profiles/"
