"""learned_params (personal scales learned from history, spec adaptive-planning D5)

Revision ID: 9c4d2a7e1b58
Revises: 5e8a1f0c2b47
Create Date: 2026-10-17 12:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c4d2a7e1b58"
down_revision: str | Sequence[str] | None = "5e8a1f0c2b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "learned_params",
        sa.Column("athlete_id", sa.Integer(), nullable=False),
        sa.Column("param", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("algo_version", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["athlete_id"], ["athletes.id"]),
        sa.PrimaryKeyConstraint("athlete_id", "param"),
    )


def downgrade() -> None:
    op.drop_table("learned_params")
