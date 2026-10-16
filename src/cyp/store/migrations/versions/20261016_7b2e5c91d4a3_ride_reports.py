"""ride_reports (per-ride PDF report delivery, cyp watch)

Revision ID: 7b2e5c91d4a3
Revises: 3c1d9e2f7a10
Create Date: 2026-10-16 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b2e5c91d4a3"
down_revision: str | Sequence[str] | None = "3c1d9e2f7a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ride_reports",
        sa.Column("activity_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("pdf_path", sa.String(length=500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.String(length=32), nullable=False),
        sa.Column("sent_at", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["activities.id"]),
        sa.PrimaryKeyConstraint("activity_id"),
    )
    with op.batch_alter_table("ride_reports", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ride_reports_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("ride_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ride_reports_status"))
    op.drop_table("ride_reports")
