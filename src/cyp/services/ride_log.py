"""RIDE.LOG text for one ride: the WORKOUT line and the metrics block (docs/specs/web-ui.md).

Deterministic only (ADR-0004): numbers come from ``activity_metrics``, the workout the athlete's
calendar showed that day (intervals.icu ``WORKOUT`` event; else our live proposal) and the
athlete's form the day before. The closing acrostic poem is written by a person (or, later, a
flagged LLM narrator) and **saved** per ride under ``data/ride_logs/<id>.txt``: a saved text wins
over the generated one everywhere (UI, ``cyp ride-log show``, Strava push).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from cyp.core.errors import NotFoundError
from cyp.services.context import AppContext
from cyp.store.models import Activity, ActivityMetrics, IcuEvent, PlannedWorkout, WellnessDaily


@dataclass(frozen=True)
class RideLog:
    """The generated blocks, plus the saved (edited, e.g. with a poem) text when there is one."""

    activity_id: int
    workout: str
    ride_log: str
    saved: str | None = None

    @property
    def generated(self) -> str:
        """Both generated blocks, ready to paste into a Strava description."""
        return f"{self.workout}\n\n{self.ride_log}"

    @property
    def text(self) -> str:
        """What to show and push: the saved text if any, else the generated one."""
        return self.saved if self.saved is not None else self.generated


def saved_path(ctx: AppContext, activity_id: int) -> Path:
    """``data/ride_logs/<activity_id>.txt``."""
    return ctx.settings.cyp_data_dir / "ride_logs" / f"{activity_id}.txt"


@contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """Serialise saves across threads and processes (UI save vs. Strava push vs. CLI)."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save(ctx: AppContext, activity_id: int, text: str) -> None:
    """Store the edited RIDE.LOG (empty text: back to the generated one).

    A different previous version is kept as ``<id>.prev.txt`` (one level of undo). Atomic
    (unique temp file + rename) and serialised by a lock file in ``data/ride_logs``.
    """
    path = saved_path(ctx, activity_id)
    with _locked(path.parent):
        if not text.strip():
            if path.is_file():
                path.replace(path.with_suffix(".prev.txt"))
            return
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if old is not None and old.strip() != text.strip():
            # Never lose a hand-written version (e.g. a poem) to a later overwrite.
            path.with_suffix(".prev.txt").write_text(old, encoding="utf-8")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{activity_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text.strip() + "\n")
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def load_saved(ctx: AppContext, activity_id: int) -> str | None:
    """The saved RIDE.LOG text, or ``None``."""
    path = saved_path(ctx, activity_id)
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def _zone_share(zones: dict[str, float] | None, keys: tuple[str, ...]) -> float | None:
    if not zones:
        return None
    total = sum(zones.values())
    return sum(zones.get(k, 0.0) for k in keys) / total if total else None


def _planned(s: Session, day: dt.date) -> tuple[str, float | None] | None:
    """(name, load) of the day's workout as the calendar showed it, else our live proposal.

    A proposal made after the day froze is never published, so the calendar is what was ridden.
    """
    events = s.scalars(
        select(IcuEvent)
        .where(IcuEvent.category == "WORKOUT", IcuEvent.start_date_local.like(f"{day}%"))
        .order_by(IcuEvent.external_id.is_(None), IcuEvent.external_id, IcuEvent.id)
    ).all()
    if events:
        e = events[0]
        return (e.name or "", e.icu_training_load)
    p = s.scalars(
        select(PlannedWorkout)
        .where(
            PlannedWorkout.date_local == day,
            PlannedWorkout.status.not_in(("cancelled", "superseded")),
        )
        .order_by(PlannedWorkout.slot)
        .limit(1)
    ).first()
    return (p.name, p.target_tss) if p is not None else None


def build(ctx: AppContext, activity_id: int) -> RideLog:
    """RIDE.LOG for a stored, analysed ride.

    Raises:
        NotFoundError: unknown activity or not analysed yet.
    """
    with ctx.factory() as s:
        a = s.get(Activity, activity_id)
        m = s.get(ActivityMetrics, activity_id)
        if a is None or m is None:
            raise NotFoundError(f"no analysed activity {activity_id}")
        day = dt.date.fromisoformat(str(a.start_local)[:10])
        planned = _planned(s, day)
        prev = s.scalars(
            select(WellnessDaily)
            .where(WellnessDaily.date_local == day - dt.timedelta(days=1))
            .limit(1)
        ).first()
        minutes = round((a.moving_s or 0) / 60)
        hr_easy = _zone_share(m.time_in_zone_hr, ("Z1",))
        tss = m.tss or a.icu_training_load
        lines = ["📋 WORKOUT"]
        if planned is not None:
            lines.append(f"課表：{planned[0]}")
        done = f"完成：{minutes} 分"
        if hr_easy is not None:
            done += f"／心率 Z1 佔 {hr_easy:.0%}"
        if m.vi is not None and m.vi >= 1.2:
            done += f"、功率 VI {m.vi:.2f} ⚠️"
        if tss is not None:
            target = f"，計畫 {planned[1]:.0f}" if planned and planned[1] else ""
            done += f"（TSS {tss:.0f}{target}）"
        lines.append(done)
        form = None
        if prev is not None and prev.ctl is not None and prev.atl is not None:
            form = round(prev.ctl - prev.atl)
        log = ["⚙️ RIDE.LOG"]
        if m.decoupling_pct is not None:
            log.append(f"DECOUPLING : {m.decoupling_pct:+.1f}%")
        if m.hr_lag_s is not None:
            log.append(f"HR_LAG : {m.hr_lag_s:.0f}s")
        if form is not None:
            log.append(f"FORM(D-1) : {form}")
        if m.status:
            log.append(f"STATUS : {m.status}")
        if m.next_recommendation:
            log.append(f"NEXT : {m.next_recommendation}")
    return RideLog(
        activity_id, "\n".join(lines), "\n".join(log), saved=load_saved(ctx, activity_id)
    )
