"""FTP: status (fast recompute), what-if, and the athlete's explicit accept."""

from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from cyp.analysis.longitudinal.ftp import FtpStatus, compute_ftp_status
from cyp.core.errors import AnalysisError
from cyp.dataset import CACHE
from cyp.schemas import FtpStatusOut
from cyp.services.context import AppContext
from cyp.store.models import Activity, AthleteSettingsHistory


def _out(status: FtpStatus, ms: float) -> FtpStatusOut:
    out = FtpStatusOut.model_validate(status.to_json())
    out.compute_ms = round(ms, 2)
    return out


def recompute(factory: sessionmaker[Session], *, as_of: dt.date) -> FtpStatus:
    """FTP status from the cached dataset (used by benchmarks and the CLI).

    Raises:
        AnalysisError: no athlete in the store.
    """
    ds = CACHE.get(factory)
    if ds is None:
        raise AnalysisError("no athlete in the store")
    return compute_ftp_status(ds, as_of)


def status(
    ctx: AppContext, *, as_of: dt.date | None = None, ftp: float | None = None
) -> FtpStatusOut:
    """Current FTP evidence; ``ftp`` evaluates the proposal against another setting (what-if)."""
    t0 = time.perf_counter()
    ds = ctx.dataset()
    st = compute_ftp_status(ds, as_of or ctx.today(), overrides={"ftp": ftp} if ftp else None)
    return _out(st, (time.perf_counter() - t0) * 1000)


def accept(
    ctx: AppContext, ftp: float, *, effective_from: dt.date | None = None, note: str | None = None
) -> FtpStatusOut:
    """Record the athlete's decision as a ``manual`` settings row.

    Rides on/after ``effective_from`` are marked ``pending_analysis`` so IF / TSS / zones are
    recomputed with the new anchor by the next ``analyze``. intervals.icu is **not** changed:
    the athlete updates their icu sport settings themselves (icu stays the load ledger).
    """
    day = effective_from or ctx.today()
    ds = ctx.dataset()
    base = ds.settings_on(day)
    with ctx.write_lock, ctx.factory() as s:
        s.add(
            AthleteSettingsHistory(
                athlete_id=ds.athlete_id,
                effective_from=day,
                ftp=float(ftp),
                lthr=int(base.lthr) if base and base.lthr else None,
                max_hr=int(base.max_hr) if base and base.max_hr else None,
                resting_hr=int(base.resting_hr) if base and base.resting_hr else None,
                weight_kg=base.weight_kg if base else ds.weight_kg,
                source="manual",
                raw_json={"note": note} if note else None,
            )
        )
        s.execute(
            update(Activity)
            .where(Activity.is_ride.is_(True), Activity.start_utc >= day.isoformat())
            .values(pending_analysis=True)
        )
        s.commit()
    return status(ctx, as_of=day)
