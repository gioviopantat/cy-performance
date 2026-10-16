"""Build the weekly / monthly digests (docs/specs/digest-reports.md).

Stub: the signatures are the contract between the job, the builders and the PDF renderer.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest
from cyp.settings import AthleteConfig
from cyp.store.streams import StreamStore


def build_weekly(
    session: Session,
    store: StreamStore,
    week_start: dt.date,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    reports_dir: Path,
) -> WeeklyDigest:
    """Read stored results for the ISO week starting ``week_start`` (a Monday).

    ``reports_dir`` is the profile's reports directory (ride facts JSON, trends JSON).
    """
    raise NotImplementedError


def build_monthly(
    session: Session,
    store: StreamStore,
    month_start: dt.date,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    reports_dir: Path,
) -> MonthlyDigest:
    """Read stored results for the calendar month starting ``month_start`` (day 1)."""
    raise NotImplementedError
