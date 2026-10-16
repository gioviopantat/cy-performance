"""Render a weekly / monthly digest to a zh-TW PDF (docs/specs/digest-reports.md).

Stub: the signature is the contract between the builders and the job.
"""

from __future__ import annotations

from pathlib import Path

from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest


def render_digest_pdf(digest: WeeklyDigest | MonthlyDigest, out_dir: Path) -> Path:
    """Write ``<out_dir>/<weekly|monthly>_<period start>.pdf`` and return its path.

    The facts ``.json`` is written beside it.
    """
    raise NotImplementedError
