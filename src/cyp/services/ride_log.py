"""RIDE.LOG text for one ride: the casual Strava post (docs/specs/casual-and-strava.md C).

The block comes from :mod:`cyp.services.ride_story` (cached story, else a deterministic one from
the ride report; no network here). A text the athlete edited is **saved** per ride under
``data/ride_logs/<id>.txt`` and wins everywhere (UI, ``cyp ride-log show``, Strava push).
"""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cyp.core.errors import NotFoundError
from cyp.services.context import AppContext
from cyp.store.models import Activity, ActivityMetrics


@dataclass(frozen=True)
class RideLog:
    """The generated block, plus the saved (edited) text when there is one."""

    activity_id: int
    #: Always empty since the casual RIDE.LOG (the API field stays for the web client).
    workout: str
    ride_log: str
    saved: str | None = None

    @property
    def generated(self) -> str:
        """The generated block, ready to paste into a Strava description."""
        return "\n\n".join(part for part in (self.workout, self.ride_log) if part)

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


def build(ctx: AppContext, activity_id: int) -> RideLog:
    """RIDE.LOG for a stored, analysed ride.

    Raises:
        NotFoundError: unknown activity or not analysed yet.
    """
    from cyp.services import ride_story

    with ctx.factory() as s:
        if s.get(Activity, activity_id) is None or s.get(ActivityMetrics, activity_id) is None:
            raise NotFoundError(f"no analysed activity {activity_id}")
    block = ride_story.story(ctx, activity_id).text
    return RideLog(activity_id, "", block, saved=load_saved(ctx, activity_id))
