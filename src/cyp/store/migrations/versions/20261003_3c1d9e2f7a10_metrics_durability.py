"""activity_metrics.durability (per-ride EF by kJ bucket)

Revision ID: 3c1d9e2f7a10
Revises: 6eba81126ed0
Create Date: 2026-10-03 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c1d9e2f7a10"
down_revision: str | Sequence[str] | None = "6eba81126ed0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("activity_metrics", schema=None) as batch_op:
        batch_op.add_column(sa.Column("durability", sa.JSON(), nullable=True))
    with op.batch_alter_table("activities", schema=None) as batch_op:
        batch_op.create_index("ix_activities_start_local", ["start_local"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("activities", schema=None) as batch_op:
        batch_op.drop_index("ix_activities_start_local")
    with op.batch_alter_table("activity_metrics", schema=None) as batch_op:
        batch_op.drop_column("durability")
