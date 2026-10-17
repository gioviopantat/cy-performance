"""digest_reports (weekly / monthly digest PDF delivery, cyp digest)

Revision ID: 5e8a1f0c2b47
Revises: 7b2e5c91d4a3
Create Date: 2026-10-17 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e8a1f0c2b47"
down_revision: str | Sequence[str] | None = "7b2e5c91d4a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_reports",
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("pdf_path", sa.String(length=500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.String(length=32), nullable=True),
        sa.Column("updated_at", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("kind", "period_start"),
    )
    with op.batch_alter_table("digest_reports", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_digest_reports_status"), ["status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("digest_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_digest_reports_status"))
    op.drop_table("digest_reports")
