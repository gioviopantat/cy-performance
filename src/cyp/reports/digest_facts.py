"""Build the weekly / monthly digests (docs/specs/digest-reports.md).

The signatures are the contract between the job, the builders and the PDF renderer. This module
only loads the period's inputs once (:class:`~cyp.reports.digest_common.DigestInputs`) and hands
them to ``digest_weekly`` / ``digest_monthly``; the sections live in ``digest_sections`` (period,
sessions, load, TID) and ``digest_progress`` (goal, recovery, next plan). Reads persisted results
only; no section computes a new model.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from cyp.core.errors import AnalysisError
from cyp.core.timeutil import DEFAULT_TZ, local_date, now_utc
from cyp.dataset import load_dataset
from cyp.planning.gate import GateDecision
from cyp.planning.season import SeasonSkeleton, build_skeleton
from cyp.reports import digest_monthly, digest_weekly
from cyp.reports.digest_common import DigestInputs
from cyp.reports.digest_model import MonthlyDigest, WeeklyDigest
from cyp.settings import AthleteConfig
from cyp.store.models import Athlete
from cyp.store.streams import StreamStore


def _inputs(
    session: Session,
    store: StreamStore,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    reports_dir: Path,
    now: dt.datetime,
    casual: frozenset[int] = frozenset(),
) -> DigestInputs:
    ds = load_dataset(session)
    if ds is None:
        raise AnalysisError("no athlete in the store; run `cyp sync` first")
    if cfg is not None:
        ds = ds.with_data_quality(cfg.data_quality.resolved())
    skeleton: SeasonSkeleton | None = None
    if cfg is not None:
        try:
            skeleton = build_skeleton(cfg)
        except ValueError:  # a season too short to plan has no phases to show
            skeleton = None
    athlete = session.get(Athlete, ds.athlete_id)
    return DigestInputs(
        session=session,
        store=store,
        cfg=cfg,
        ds=ds,
        profile=profile,
        athlete_name=athlete.name if athlete is not None and athlete.name else profile,
        reports_dir=reports_dir,
        today=local_date(now, cfg.timezone if cfg is not None else DEFAULT_TZ),
        skeleton=skeleton,
        casual=casual,
    )


def build_weekly(
    session: Session,
    store: StreamStore,
    week_start: dt.date,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    reports_dir: Path,
    gate: GateDecision | None = None,
    casual: frozenset[int] = frozenset(),
) -> WeeklyDigest:
    """Read stored results for the ISO week starting ``week_start`` (a Monday).

    ``reports_dir`` is the profile's reports directory (ride facts JSON, trends JSON);
    ``gate`` the progression gate's decision for the week after (flag-gated by the caller);
    ``casual`` the rides the athlete tagged casual (not graded).

    Raises:
        AnalysisError: no athlete in the store.
        ValueError: ``week_start`` is not a Monday.
    """
    if week_start.weekday() != 0:
        raise ValueError(f"{week_start} is not a Monday")
    now = now_utc()
    inp = _inputs(
        session, store, profile=profile, cfg=cfg, reports_dir=reports_dir, now=now, casual=casual
    )
    return digest_weekly.build(inp, week_start, generated_at=now, gate=gate)


def build_monthly(
    session: Session,
    store: StreamStore,
    month_start: dt.date,
    *,
    profile: str,
    cfg: AthleteConfig | None,
    reports_dir: Path,
    casual: frozenset[int] = frozenset(),
) -> MonthlyDigest:
    """Read stored results for the calendar month starting ``month_start`` (day 1).

    ``casual``: the rides the athlete tagged casual (not graded).

    Raises:
        AnalysisError: no athlete in the store.
        ValueError: ``month_start`` is not the first of a month.
    """
    if month_start.day != 1:
        raise ValueError(f"{month_start} is not the first of a month")
    now = now_utc()
    inp = _inputs(
        session, store, profile=profile, cfg=cfg, reports_dir=reports_dir, now=now, casual=casual
    )
    return digest_monthly.build(inp, month_start, generated_at=now)
