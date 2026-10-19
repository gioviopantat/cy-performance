"""``athletes`` upsert + ``athlete_settings_history`` append-if-changed repository."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from cyp.core.timeutil import iso_utc, now_utc
from cyp.store.models import Athlete, AthleteSettingsHistory
from cyp.store.repo.base import Repo

#: Columns compared to decide whether a new history row is needed.
COMPARED_FIELDS: tuple[str, ...] = (
    "ftp",
    "indoor_ftp",
    "eftp",
    "w_prime",
    "p_max",
    "lthr",
    "max_hr",
    "resting_hr",
    "weight_kg",
    "power_zones",
    "hr_zones",
)


class AthleteSettingsRepo(Repo):
    """Single-athlete upsert and the settings timeline (docs/03 ``athlete_settings_history``)."""

    # ------------------------------------------------------------------ athletes

    def get_by_intervals_id(self, intervals_id: str) -> Athlete | None:
        """Fetch the athlete row by icu id."""
        return self.session.scalars(
            select(Athlete).where(Athlete.intervals_id == intervals_id)
        ).first()

    def upsert_athlete(self, intervals_id: str, values: dict[str, Any]) -> Athlete:
        """Insert or update the athlete identified by ``intervals_id``.

        Single-athlete system: if no row carries this icu id but exactly one athlete exists
        without one (e.g. created by the Strava sync first), that row is claimed.
        """
        row = self.get_by_intervals_id(intervals_id)
        if row is None:
            orphans = list(
                self.session.scalars(select(Athlete).where(Athlete.intervals_id.is_(None)))
            )
            if len(orphans) == 1:
                row = orphans[0]
                row.intervals_id = intervals_id
        if row is None:
            row = Athlete(intervals_id=intervals_id)
            self.session.add(row)
        for key, val in values.items():
            if key in {"id", "intervals_id"}:
                continue
            setattr(row, key, val)
        row.updated_at = iso_utc(now_utc())
        self.flush()
        return row

    # ------------------------------------------------------------------ history

    def latest(self, athlete_id: int, source: str | None = None) -> AthleteSettingsHistory | None:
        """Most recent history row (optionally for one ``source``)."""
        stmt = (
            select(AthleteSettingsHistory)
            .where(AthleteSettingsHistory.athlete_id == athlete_id)
            .order_by(
                AthleteSettingsHistory.effective_from.desc(), AthleteSettingsHistory.id.desc()
            )
            .limit(1)
        )
        if source:
            stmt = stmt.where(AthleteSettingsHistory.source == source)
        return self.session.scalars(stmt).first()

    def effective_on(
        self, athlete_id: int, day: dt.date, source: str | None = None
    ) -> AthleteSettingsHistory | None:
        """Latest row with ``effective_from <= day`` (optionally for one ``source``)."""
        stmt = (
            select(AthleteSettingsHistory)
            .where(
                AthleteSettingsHistory.athlete_id == athlete_id,
                AthleteSettingsHistory.effective_from <= day,
            )
            .order_by(
                AthleteSettingsHistory.effective_from.desc(), AthleteSettingsHistory.id.desc()
            )
            .limit(1)
        )
        if source:
            stmt = stmt.where(AthleteSettingsHistory.source == source)
        return self.session.scalars(stmt).first()

    def append_if_changed(
        self,
        athlete_id: int,
        effective_from: dt.date,
        values: dict[str, Any],
        *,
        source: str,
        raw_json: dict[str, Any] | None = None,
    ) -> AthleteSettingsHistory | None:
        """Add a history row when any compared field differs from the latest row of ``source``.

        Returns the new row, or ``None`` when nothing changed (the latest row's ``raw_json`` is
        refreshed in that case so it always reflects the most recent payload).
        """
        current = self.latest(athlete_id, source)
        if current is not None and all(
            getattr(current, f) == values.get(f) for f in COMPARED_FIELDS
        ):
            if raw_json is not None:
                current.raw_json = raw_json
                self.flush()
            return None
        row = AthleteSettingsHistory(
            athlete_id=athlete_id,
            effective_from=effective_from,
            source=source,
            raw_json=raw_json,
        )
        for f in COMPARED_FIELDS:
            setattr(row, f, values.get(f))
        self.session.add(row)
        self.flush()
        return row

    def history(self, athlete_id: int, source: str | None = None) -> list[AthleteSettingsHistory]:
        """All history rows ascending by ``effective_from``."""
        stmt = (
            select(AthleteSettingsHistory)
            .where(AthleteSettingsHistory.athlete_id == athlete_id)
            .order_by(AthleteSettingsHistory.effective_from, AthleteSettingsHistory.id)
        )
        if source:
            stmt = stmt.where(AthleteSettingsHistory.source == source)
        return list(self.session.scalars(stmt))
