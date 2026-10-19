"""ride_reports.strava_status / strava_error (automatic Strava RIDE.LOG, ADR-0020)

Revision ID: b3f6d0a8c214
Revises: 9c4d2a7e1b58
Create Date: 2026-10-17 18:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f6d0a8c214"
down_revision: str | Sequence[str] | None = "9c4d2a7e1b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ride_reports") as batch:
        batch.add_column(sa.Column("strava_status", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("strava_error", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ride_reports") as batch:
        batch.drop_column("strava_error")
        batch.drop_column("strava_status")
