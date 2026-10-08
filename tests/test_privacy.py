"""Personal data never enters the public repo (ADR-0006 §8).

Real athlete files live in ``profiles/<slug>/`` (git-ignored). If this fails, run
``git rm --cached <path>`` and move the file under ``profiles/``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FILES = {"config/athlete.yaml", ".env"}
FORBIDDEN_DIRS = ("profiles/", "data/")
#: sha256 of real athlete identifiers (Strava / intervals.icu ids) that once leaked or could.
#: Stored as hashes so the test itself does not publish them.
DENIED_ID_SHA256 = {
    "c6799e7a0e457e1285705e4a37e474e67011cc11d984cce36e011d3239b17b61",
    "3a31e8699be46779a395e58410f7ec455cca22b7f0efde03fdcdb3b257ffac94",
    "b1405f6c914b649fb1cfcdd0de290b73595f363ff74730766e23240033061828",
}
ID_TOKEN = re.compile(r"(?<![\w.])i?\d{6,10}(?![\w.])")
SKIP_SUFFIXES = {".lock", ".png", ".jpg", ".ico", ".parquet", ".sqlite"}


def _tracked(*extra: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", *extra], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.stdout.splitlines()


def test_no_personal_files_tracked() -> None:
    bad = [p for p in _tracked() if p in FORBIDDEN_FILES or p.startswith(FORBIDDEN_DIRS)]
    assert not bad, f"personal files tracked in the public repo: {bad}; move them to profiles/"


def test_no_real_athlete_ids_in_the_repo() -> None:
    """Tracked and new (not ignored) files never contain a denied athlete id."""
    hits = []
    for rel in _tracked("--cached", "--others", "--exclude-standard"):
        path = ROOT / rel
        if path.suffix in SKIP_SUFFIXES or rel.endswith("package-lock.json") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for token in set(ID_TOKEN.findall(text)):
            if hashlib.sha256(token.encode()).hexdigest() in DENIED_ID_SHA256:
                hits.append(rel)
    assert not hits, f"a real athlete id appears in: {sorted(set(hits))}; use an invented id"
