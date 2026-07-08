"""Add outcome tracking fields to owner_decisions

Revision ID: a1b2c3d4e5f6
Revises: d4e2f1a8b753
Create Date: 2026-04-02 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d4e2f1a8b753"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "owner_decisions",
        sa.Column("resolution_quality", sa.String(20), nullable=True),
    )
    op.add_column(
        "owner_decisions",
        sa.Column("estimated_revenue_saved", sa.Float, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("owner_decisions", "estimated_revenue_saved")
    op.drop_column("owner_decisions", "resolution_quality")
